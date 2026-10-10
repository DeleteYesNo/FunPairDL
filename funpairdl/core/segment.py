from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
from pathlib import Path
from typing import Callable

import aiohttp

from funpairdl.constants import CHUNK_SIZE, SEGMENT_SOCK_READ

logger = logging.getLogger("funpairdl.segment")

# Buffer size before flushing to disk in a background thread.
# Larger buffer = fewer disk writes = less SSD pressure.
# With 32 segments, worst case = 32 × 4 MB = 128 MB in flight — acceptable.
_FLUSH_SIZE = 4 * 1024 * 1024  # 4 MB

# Limit concurrent disk writes across ALL segments.
# Segments keep downloading at full speed (data buffered in memory)
# but only a few flush to disk at once, preventing DRAM-less SSDs
# (like BX500) from hitting 100% disk usage.
_disk_write_sem = threading.Semaphore(4)


async def _read_error_detail(resp) -> str:
    """Extract a human-readable reason from a small JSON error body.

    Hosts like pixeldrain answer 4xx with {"value": ..., "message": ...}
    explaining WHY (rate limit, captcha, virus flag); without it the user
    only sees an opaque status code. Anything unreadable is ignored.
    """
    import json

    try:
        stream = resp.content
        raw = b""
        while len(raw) < 512:
            chunk = await stream.read(512 - len(raw))
            if not chunk:  # EOF
                break
            raw += chunk
        body = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return ""
    if isinstance(body, dict):
        parts = [str(body[key]) for key in ("value", "message") if body.get(key)]
        return " — ".join(parts)
    return ""


class SegmentDownloader:
    """Downloads a single byte-range segment of a file."""

    def __init__(
        self,
        url: str,
        range_start: int,
        range_end: int,
        temp_file: Path,
        index: int,
        headers: dict[str, str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        use_range: bool = True,
        known_total: int = 0,
    ):
        self.url = url
        self.range_start = range_start
        self.range_end = range_end
        self.temp_file = temp_file
        self.index = index
        self.headers = headers or {}
        self.on_progress = on_progress
        self.use_range = use_range
        # Real total file size, when the caller knows it. For no-range
        # downloads this is the ONLY trustworthy completeness reference —
        # range_start/range_end are placeholder zeros there.
        self.known_total = known_total

        self.downloaded: int = 0
        self._cancelled = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._last_progress_time: float = 0

    @property
    def expected(self) -> int:
        """Bytes this segment must hold when done (0 = unknown)."""
        if not self.use_range:
            # No-range plans carry placeholder range_end=0; only a
            # caller-supplied real total says how big the file is (audit [14]).
            return max(self.known_total, 0)
        return self.range_end - self.range_start + 1

    @property
    def is_complete(self) -> bool:
        # Exactly the expected size: a bigger file is not "complete and then
        # some" but bytes from another plan or another file — an earlier
        # attempt that split the file differently left a longer .part, which
        # used to be merged as-is and gave a larger, corrupt video.
        return self.expected > 0 and self.downloaded == self.expected

    def _discard(self, why: str) -> None:
        logger.warning("Segment %d: discarding %s (%s)", self.index, self.temp_file.name, why)
        try:
            self.temp_file.unlink(missing_ok=True)
        except OSError:
            pass
        self.downloaded = 0

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._pause_event.set()  # unblock if paused

    async def download(self, session: aiohttp.ClientSession) -> None:
        # Check for existing partial download
        if self.temp_file.exists():
            size = self.temp_file.stat().st_size
            self.downloaded = size
            if self.is_complete:
                logger.debug("Segment %d already complete", self.index)
                return
            if self.expected and size > self.expected:
                self._discard(f"{size} bytes, segment holds {self.expected}")
            elif not self.use_range:
                # No-range downloads can't resume — the server always sends
                # the file from byte 0, so any leftover partial (failed
                # attempt, pause, stale .part0 from a previous run) is
                # unresumable (audit [14]).
                self._discard(f"unresumable partial, {size} bytes")
            # else: a shorter ranged partial — resume from where it left off

        async def _attempt(verify_tls: bool) -> None:
            if not self.use_range and self.downloaded > 0:
                # A failed earlier attempt (e.g. the verified-TLS try, or a
                # previous retry round) left partial state. No-range requests
                # restart from byte 0, so drop the partial instead of
                # appending a second copy of the file to it.
                try:
                    self.temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
                self.downloaded = 0
            actual_start = self.range_start + self.downloaded
            req_headers = {**self.headers}
            if self.use_range:
                req_headers["Range"] = f"bytes={actual_start}-{self.range_end}"

            logger.debug(
                "Segment %d: downloading bytes %d-%d",
                self.index, actual_start, self.range_end,
            )

            # Initialised before the try so the except/flush paths can reference
            # them even when the connection itself fails (e.g. a TLS error)
            # before the streaming loop — otherwise we'd raise an
            # UnboundLocalError that masks the real cause.
            buf = bytearray()
            file_path = self.temp_file
            mode = "ab" if self.downloaded > 0 else "wb"
            first_flush = True

            try:
                expected_statuses = (200, 206) if self.use_range else (200,)
                async with session.get(
                    self.url, headers=req_headers, ssl=verify_tls,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=SEGMENT_SOCK_READ),
                ) as resp:
                    if resp.status not in expected_statuses:
                        # 4xx (except 429) are permanent — don't retry
                        if 400 <= resp.status < 500 and resp.status != 429:
                            detail = await _read_error_detail(resp)
                            raise RuntimeError(
                                f"HTTP {resp.status} (permanent) for segment {self.index}"
                                + (f": {detail}" if detail else "")
                            )
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message=f"Unexpected status {resp.status} for segment {self.index}",
                        )

                    if self.use_range and resp.status == 200 and actual_start > 0:
                        # The whole file from byte 0 would land at this
                        # segment's offset.
                        raise RuntimeError(
                            f"Segment {self.index}: server ignored Range "
                            f"(HTTP 200 for bytes={actual_start}-)"
                        )

                    self.temp_file.parent.mkdir(parents=True, exist_ok=True)

                    # Buffer writes and flush to disk in a background thread
                    # to avoid blocking the Qt/async event loop.
                    def _flush(data: bytes, fpath, wmode: str):
                        with _disk_write_sem:
                            with open(fpath, wmode) as f:
                                f.write(data)

                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        if self._cancelled:
                            logger.debug("Segment %d cancelled", self.index)
                            break

                        # Wait if paused
                        await self._pause_event.wait()

                        if self._cancelled:
                            break

                        buf.extend(chunk)
                        self.downloaded += len(chunk)

                        if len(buf) >= _FLUSH_SIZE:
                            wm = mode if first_flush else "ab"
                            await asyncio.to_thread(_flush, bytes(buf), file_path, wm)
                            buf.clear()
                            first_flush = False

                        if self.on_progress:
                            now = time.monotonic()
                            if now - self._last_progress_time >= 0.15:
                                self._last_progress_time = now
                                self.on_progress(self.index, self.downloaded)

                    # Final flush
                    if buf and not self._cancelled:
                        wm = mode if first_flush else "ab"
                        await asyncio.to_thread(_flush, bytes(buf), file_path, wm)
                        buf.clear()  # written: the error path below must not write it again

                    # Always report final progress so download_task sees 100%
                    if self.on_progress and not self._cancelled:
                        self.on_progress(self.index, self.downloaded)

                    # A stream that ends without an error is not proof the
                    # segment is whole. Short: raise, and the retry resumes
                    # from here. Long: those bytes belong elsewhere — drop.
                    if not self._cancelled and self.expected:
                        if self.downloaded > self.expected:
                            got = self.downloaded
                            self._discard(f"received {got} bytes, segment holds {self.expected}")
                            raise RuntimeError(
                                f"Segment {self.index}: received {got} bytes, expected {self.expected}"
                            )
                        if self.downloaded < self.expected:
                            raise RuntimeError(
                                f"Segment {self.index}: stream ended at {self.downloaded}"
                                f"/{self.expected} bytes"
                            )

            except asyncio.CancelledError:
                # Best-effort flush so retries resume from where we left off
                if buf:
                    try:
                        wm = mode if first_flush else "ab"
                        await asyncio.to_thread(_flush, bytes(buf), file_path, wm)
                    except Exception:
                        pass
                logger.debug("Segment %d task cancelled", self.index)
                raise
            except Exception as e:
                # Best-effort flush so retries resume from where we left off
                # instead of losing up to 4 MB of buffered data per segment.
                if buf:
                    try:
                        wm = mode if first_flush else "ab"
                        await asyncio.to_thread(_flush, bytes(buf), file_path, wm)
                    except Exception:
                        pass
                logger.error("Segment %d error: %s", self.index, e)
                raise

        # Verify TLS certs by default; only if a host presents a broken cert
        # (expired/misconfigured) do we retry THAT host without verification.
        # Hosts with valid certs (pixeldrain, mega, …) keep full verification.
        try:
            await _attempt(True)
        except (aiohttp.ClientSSLError, ssl.SSLError) as e:
            logger.warning(
                "Segment %d: TLS certificate rejected (%s) — retrying this host "
                "without certificate verification",
                self.index, e,
            )
            await _attempt(False)
