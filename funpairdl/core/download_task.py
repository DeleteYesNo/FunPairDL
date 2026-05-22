from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

import aiohttp

from funpairdl.constants import (
    CHUNK_SIZE,
    DEFAULT_SEGMENTS,
    MIN_SEGMENT_SIZE,
    PROGRESS_UPDATE_INTERVAL,
    SEGMENT_MAX_ROUNDS,
    SEGMENT_RETRY_DELAYS,
    SMALL_FILE_THRESHOLD,
)
from funpairdl.core.pair import ItemState, PairItem, SegmentInfo
from funpairdl.core.progress import SpeedCalculator
from funpairdl.core.retry import retry_async
from funpairdl.core.segment import SegmentDownloader

logger = logging.getLogger("funpairdl.download_task")


class DownloadTask:
    """Orchestrates multi-segment download of a single file."""

    def __init__(
        self,
        item: PairItem,
        output_dir: Path,
        num_segments: int = DEFAULT_SEGMENTS,
        on_progress: Callable[[PairItem], None] | None = None,
        on_state_change: Callable[[PairItem], None] | None = None,
    ):
        self.item = item
        self.output_dir = output_dir
        self.num_segments = num_segments
        self.on_progress = on_progress
        self.on_state_change = on_state_change

        self._segments: list[SegmentDownloader] = []
        self._speed_calc = SpeedCalculator()
        self._cancelled = False
        self._paused = False
        self._last_progress_time: float = 0

    @property
    def parts_dir(self) -> Path:
        return self.output_dir / ".parts"

    @property
    def output_file(self) -> Path:
        return self.output_dir / self.item.filename

    def _set_state(self, state: ItemState, error: str = "") -> None:
        self.item.state = state
        self.item.error_message = error
        if self.on_state_change:
            self.on_state_change(self.item)

    def _update_progress(self, segment_index: int, segment_downloaded: int) -> None:
        # Update total downloaded from all segments.
        # Use max() so progress never goes backward — a segment retry may
        # briefly report a lower value until it catches up from disk.
        total_downloaded = sum(s.downloaded for s in self._segments)
        self.item.downloaded_bytes = max(self.item.downloaded_bytes, total_downloaded)

        # Throttle progress updates
        now = time.monotonic()
        if now - self._last_progress_time < PROGRESS_UPDATE_INTERVAL:
            return
        self._last_progress_time = now

        self._speed_calc.update(total_downloaded)
        self.item.speed_bps = self._speed_calc.speed_bps

        if self.on_progress:
            self.on_progress(self.item)

    async def _resolve_url(self, session: aiohttp.ClientSession) -> None:
        """Resolve the download URL and get file info via HEAD request."""
        self._set_state(ItemState.RESOLVING)

        url = self.item.resolved_url or self.item.url
        headers = {**self.item.headers}

        try:
            async with session.head(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    # 4xx (except 429) are permanent errors — don't retry
                    if 400 <= resp.status < 500 and resp.status != 429:
                        raise RuntimeError(
                            f"HTTP {resp.status} (permanent): {url[:100]}"
                        )
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status,
                        message=f"HEAD request failed: {resp.status}",
                    )

                self.item.resolved_url = str(resp.url)
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self.item.total_bytes = int(content_length)

                # Check range support
                accept_ranges = resp.headers.get("Accept-Ranges", "")
                supports_range = accept_ranges.lower() == "bytes"

                # Try to get filename from Content-Disposition
                cd = resp.headers.get("Content-Disposition", "")
                if cd:
                    from funpairdl.utils.filename import parse_content_disposition, sanitize_filename
                    fname = parse_content_disposition(cd)
                    if fname:
                        self.item.filename = sanitize_filename(fname)

                return supports_range

        except Exception as e:
            # If HEAD fails but we have a resolved URL from provider,
            # proceed with single-segment download (size may be unknown)
            if self.item.resolved_url:
                logger.warning(
                    "HEAD failed for %s but have resolved URL, proceeding: %s",
                    url, e,
                )
                return False  # no range support assumed
            logger.error("Failed to resolve URL %s: %s", url, e)
            raise

    def _plan_segments(self, supports_range: bool) -> list[SegmentDownloader]:
        total = self.item.total_bytes
        url = self.item.resolved_url or self.item.url

        # Single segment for small files or no range support
        if not supports_range or total <= 0 or total < SMALL_FILE_THRESHOLD:
            num = 1
        else:
            num = min(self.num_segments, max(1, total // MIN_SEGMENT_SIZE))

        segments = []
        if num == 1:
            # If no range support or unknown size, use_range=False
            use_range = supports_range and total > 0
            seg = SegmentDownloader(
                url=url,
                range_start=0,
                range_end=max(total - 1, 0) if use_range else 0,
                temp_file=self.parts_dir / f"{self.item.filename}.part0",
                index=0,
                headers=self.item.headers,
                on_progress=self._update_progress,
                use_range=use_range,
            )
            segments.append(seg)
        else:
            segment_size = total // num
            for i in range(num):
                start = i * segment_size
                end = (i + 1) * segment_size - 1 if i < num - 1 else total - 1
                seg = SegmentDownloader(
                    url=url,
                    range_start=start,
                    range_end=end,
                    temp_file=self.parts_dir / f"{self.item.filename}.part{i}",
                    index=i,
                    headers=self.item.headers,
                    on_progress=self._update_progress,
                )
                segments.append(seg)

        return segments

    async def _merge_segments(self) -> None:
        """Merge all segment temp files into the final output file.

        Runs file I/O in a thread to avoid blocking the event loop.
        """
        output_dir = self.output_dir
        output = self.output_file
        parts_dir = self.parts_dir
        segments = self._segments

        def _do_merge():
            output_dir.mkdir(parents=True, exist_ok=True)
            with open(output, "wb") as out_f:
                for seg in segments:
                    with open(seg.temp_file, "rb") as in_f:
                        while True:
                            chunk = in_f.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            out_f.write(chunk)
            # Clean up temp files (but keep .parts dir — other items may still use it)
            for seg in segments:
                try:
                    seg.temp_file.unlink(missing_ok=True)
                except OSError:
                    pass

        await asyncio.to_thread(_do_merge)

    async def download(self, session: aiohttp.ClientSession) -> None:
        """Execute the full download pipeline."""
        try:
            # Resolve URL and file info
            supports_range = await retry_async(
                self._resolve_url, session, max_retries=3,
                retryable_exceptions=(aiohttp.ClientError, asyncio.TimeoutError),
            )

            if self._cancelled:
                return

            # Plan and create segments
            self._segments = self._plan_segments(supports_range)
            self.item.segments = [
                SegmentInfo(
                    index=s.index,
                    range_start=s.range_start,
                    range_end=s.range_end,
                    temp_file=str(s.temp_file),
                )
                for s in self._segments
            ]

            self._set_state(ItemState.DOWNLOADING)
            self._speed_calc.reset()

            # Download all segments with round-based retry.
            # Each round only retries segments that failed in the previous
            # round — completed segments are preserved on disk and never
            # re-downloaded.  This is far more resilient than the old
            # approach where one bad segment killed the entire item.
            await self._download_segments(session)

            if self._cancelled:
                return

            # Merge segments
            await self._merge_segments()

            # Final progress update — handle unknown file size
            if self.item.total_bytes <= 0:
                self.item.total_bytes = self.item.downloaded_bytes
            self.item.downloaded_bytes = self.item.total_bytes
            self.item.speed_bps = 0
            self._set_state(ItemState.COMPLETED)

            logger.info("Download complete: %s", self.item.filename)

        except asyncio.CancelledError:
            self._set_state(ItemState.PAUSED if self._paused else ItemState.FAILED)
            raise
        except Exception as e:
            logger.error("Download failed for %s: %s", self.item.filename, e)
            self._set_state(ItemState.FAILED, str(e))
            raise

    async def _download_segments(self, session: aiohttp.ClientSession) -> None:
        """Download all segments with round-based retry.

        Only failed segments are retried each round.  Completed segments
        stay on disk and are never touched again, so a transient CDN hiccup
        on one segment doesn't throw away the work done by the other 15.
        """
        pending = list(self._segments)
        last_error: Exception | None = None

        for round_idx in range(SEGMENT_MAX_ROUNDS):
            if not pending or self._cancelled:
                break

            tasks = [asyncio.create_task(seg.download(session)) for seg in pending]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            failed: list[SegmentDownloader] = []
            for seg, result in zip(pending, results):
                if result is None:
                    continue  # segment completed OK
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    last_error = result
                    # Permanent HTTP errors (4xx except 429) — abort immediately
                    if isinstance(result, RuntimeError) and "(permanent)" in str(result):
                        raise result
                    logger.warning(
                        "Segment %d failed (round %d/%d): %s",
                        seg.index, round_idx + 1, SEGMENT_MAX_ROUNDS, result,
                    )
                    failed.append(seg)

            # Update pending BEFORE break so the final check is accurate
            pending = failed
            if not pending:
                break
            if round_idx < SEGMENT_MAX_ROUNDS - 1:
                delay = SEGMENT_RETRY_DELAYS[min(round_idx, len(SEGMENT_RETRY_DELAYS) - 1)]
                logger.info(
                    "Retrying %d/%d failed segment(s) in %ds… [%s]",
                    len(failed), len(self._segments), delay, self.item.filename,
                )
                await asyncio.sleep(delay)

        if pending and not self._cancelled:
            raise RuntimeError(
                f"{len(pending)}/{len(self._segments)} segment(s) still failing "
                f"after {SEGMENT_MAX_ROUNDS} rounds: {last_error}"
            )

    def pause(self) -> None:
        self._paused = True
        for seg in self._segments:
            seg.pause()
        self._set_state(ItemState.PAUSED)

    def resume(self) -> None:
        self._paused = False
        for seg in self._segments:
            seg.resume()
        self._set_state(ItemState.DOWNLOADING)

    def cancel(self) -> None:
        self._cancelled = True
        for seg in self._segments:
            seg.cancel()
