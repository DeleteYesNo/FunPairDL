from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Callable

import aiohttp

from funpairdl.constants import CHUNK_SIZE, DEFAULT_DOWNLOAD_DIR, DEFAULT_SEGMENTS
from funpairdl.core.download_task import DownloadTask
from funpairdl.core.pair import (
    FileType,
    ItemState,
    Pair,
    PairItem,
    PairState,
)
from funpairdl.providers.base import ResolvedFile
from funpairdl.providers.registry import ProviderRegistry
from funpairdl.utils.filename import sanitize_filename
from funpairdl.utils.url_parser import detect_provider

logger = logging.getLogger("funpairdl.queue_manager")


class QueueManager:
    """Manages the download queue. Pairs download sequentially;
    items within a pair download concurrently."""

    def __init__(
        self,
        download_dir: Path = DEFAULT_DOWNLOAD_DIR,
        num_segments: int = DEFAULT_SEGMENTS,
    ):
        self.download_dir = download_dir
        self.num_segments = num_segments
        self.pairs: list[Pair] = []
        self._current_tasks: list[asyncio.Task] = []      # All active asyncio tasks (across all pairs)
        self._download_tasks: list[DownloadTask] = []      # All active DownloadTask objects (across all pairs)
        self._pair_tasks: dict[str, list[asyncio.Task]] = {}  # pair_id → its asyncio tasks (for pause/cancel)
        self._running = False
        self._pump_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._registry: ProviderRegistry | None = None

        self._pump_heartbeat: float = 0  # monotonic timestamp of last pump activity
        self._pump_wake = asyncio.Event()  # signal pump to check for new work

        # Dedicated download thread with its own asyncio event loop
        # Keeps all download I/O off the Qt main thread
        self._dl_loop: asyncio.AbstractEventLoop | None = None
        self._dl_thread: threading.Thread | None = None

        # Callbacks for GUI integration
        self.on_pair_added: Callable[[Pair], None] | None = None
        self.on_pair_updated: Callable[[Pair], None] | None = None
        self.on_item_updated: Callable[[PairItem], None] | None = None
        self.on_queue_changed: Callable[[], None] | None = None
        self.on_save_needed: Callable[[], None] | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Create a dedicated download thread with its own asyncio event loop.
        # All download I/O runs here, keeping the Qt main thread responsive.
        self._dl_loop = asyncio.new_event_loop()
        self._dl_thread = threading.Thread(
            target=self._dl_loop.run_forever,
            name="dl-thread",
            daemon=True,
        )
        self._dl_thread.start()

        # Initialize session and pump inside the download thread
        asyncio.run_coroutine_threadsafe(self._dl_init(), self._dl_loop)
        logger.info("QueueManager started (dedicated download thread)")

    async def _dl_init(self) -> None:
        """Initialize download resources — runs in download thread."""
        from funpairdl.constants import BROWSER_USER_AGENT

        connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        # Recreate Event in the download thread's loop context
        self._pump_wake = asyncio.Event()
        self._pump_task = asyncio.create_task(self._pump_with_watchdog())
        logger.info("Download thread initialized")

    def _ensure_pump_alive(self) -> None:
        """Check if the pump task is still running; restart if dead or hung.

        Called from the main thread — dispatches actual check to the download thread.
        """
        if not self._running or not self._dl_loop or not self._dl_loop.is_running():
            return
        self._dl_loop.call_soon_threadsafe(self._check_pump_health)

    def _check_pump_health(self) -> None:
        """Verify pump health — runs in the download thread."""
        import time
        if self._pump_task is None or self._pump_task.done():
            exc = self._pump_task.exception() if self._pump_task and not self._pump_task.cancelled() else None
            logger.warning("Pump task is dead (exception=%s), restarting!", exc)
            self._pump_task = self._dl_loop.create_task(self._pump_with_watchdog())
            return

        # Check for hung pump: no heartbeat for 2+ minutes while pairs are queued
        if self._pump_heartbeat > 0:
            elapsed = time.monotonic() - self._pump_heartbeat
            has_queued = any(p.state == PairState.QUEUED for p in self.pairs)
            if elapsed > 120 and has_queued:
                logger.warning(
                    "Pump appears hung (no heartbeat for %.0fs with %d queued pairs), force-restarting!",
                    elapsed, sum(1 for p in self.pairs if p.state == PairState.QUEUED),
                )
                self._pump_task.cancel()
                self._pump_task = self._dl_loop.create_task(self._pump_with_watchdog())

    async def force_restart_pump(self) -> None:
        """Force-cancel current downloads and restart the pump.

        Called from the main thread (GUI/API) — dispatches to download thread.
        """
        if self._dl_loop and self._dl_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._force_restart_pump_impl(), self._dl_loop
            )
            await asyncio.wrap_future(future)
        else:
            await self._force_restart_pump_impl()

    async def _force_restart_pump_impl(self) -> None:
        """Force restart implementation — runs in download thread."""
        import time
        logger.warning("Force-restarting pump (manual trigger)")

        # Cancel all active download tasks
        for task in self._current_tasks:
            task.cancel()
        self._current_tasks.clear()
        self._download_tasks.clear()

        # Cancel the pump task itself
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass

        # Reset stuck DOWNLOADING pairs back to QUEUED
        for pair in self.pairs:
            if pair.state == PairState.DOWNLOADING:
                pair.state = PairState.QUEUED
                for item in pair.items:
                    if item.state in (ItemState.RESOLVING, ItemState.DOWNLOADING):
                        item.state = ItemState.PENDING
                        item.error_message = ""
                if self.on_pair_updated:
                    self.on_pair_updated(pair)

        # Restart the pump
        self._pump_heartbeat = time.monotonic()
        self._pump_wake = asyncio.Event()
        self._pump_wake.set()  # wake immediately to process queued pairs
        self._pump_task = asyncio.create_task(self._pump_with_watchdog())
        logger.info("Pump restarted successfully")

    async def _pump_with_watchdog(self) -> None:
        """Wrapper that auto-restarts the pump if it crashes unexpectedly."""
        while self._running:
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.critical("Pump loop crashed unexpectedly: %s", e, exc_info=True)
                if self._running:
                    logger.info("Restarting pump in 2 seconds...")
                    await asyncio.sleep(2)

    async def stop(self) -> None:
        self._running = False
        if self._dl_loop and self._dl_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._dl_shutdown(), self._dl_loop)
            try:
                await asyncio.wrap_future(future)
            except Exception as e:
                logger.error("Download thread shutdown error: %s", e)
            self._dl_loop.call_soon_threadsafe(self._dl_loop.stop)
            # Thread is daemon — will die with process, no need to join
        logger.info("QueueManager stopped")

    async def _dl_shutdown(self) -> None:
        """Shutdown download resources — runs in download thread."""
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        for task in self._current_tasks:
            task.cancel()
        if self._session:
            await self._session.close()

    def add_pair(
        self,
        name: str,
        video_urls: list[str] | None = None,
        script_urls: list[str] | None = None,
        preferred_resolution: str = "best",
        script_authors: dict[str, str] | None = None,
        auto_rename: bool = True,
        output_dir_override: str = "",
        groups: list[dict] | None = None,
    ) -> Pair:
        """Add a Pair to the queue.

        Either pass `video_urls`/`script_urls` (everything lands in the Main
        group → root folder, legacy behavior), or pass `groups` — a list of
        dicts shaped like the PairGroupSpec schema. When `groups` is set,
        the flat `*_urls` arguments are ignored.

        Group entries:
          {
            "name": "Main" | "Alt 1" | ...,
            "video_urls": [...], "script_urls": [...],
            "script_authors": {url: author},
            "inherit_multi_axis": bool,   # only meaningful for Alt groups
          }
        """
        # Normalize input: groups[] takes priority; otherwise wrap flat lists
        # as a single Main group so downstream code only handles one shape.
        if groups is None:
            groups = [{
                "name": "Main",
                "video_urls": video_urls or [],
                "script_urls": script_urls or [],
                "script_authors": script_authors or {},
                "inherit_multi_axis": False,
            }]

        pair = Pair(name=name, preferred_resolution=preferred_resolution, auto_rename=auto_rename)

        folder_name = sanitize_filename(self._clean_title(name))
        # Caller-supplied override (e.g. Pixeldrain picker) lets a single
        # batch land somewhere other than the global default — useful when
        # the default volume is full.
        root = Path(output_dir_override) if output_dir_override else self.download_dir
        target_dir = root / folder_name

        # Check if this pair is already in the queue (prevent duplicate submissions)
        existing = next(
            (p for p in self.pairs
             if p.output_dir == str(target_dir) and p.state != PairState.COMPLETED),
            None,
        )
        if existing:
            if existing.state == PairState.FAILED:
                # Re-queue the failed pair instead of creating a duplicate
                existing.state = PairState.QUEUED
                for item in existing.items:
                    if item.state == ItemState.FAILED:
                        item.state = ItemState.PENDING
                        item.error_message = ""
                logger.info("Re-queuing failed pair: %s", name)
                if self.on_pair_updated:
                    self.on_pair_updated(existing)
                self._ensure_pump_alive()
                self._wake_pump()
            else:
                logger.info("Pair '%s' already in queue, skipping", name)
            return existing

        pair.output_dir = str(target_dir)

        for grp in groups:
            grp_name = grp.get("name", "Main") or "Main"
            grp_videos = grp.get("video_urls") or []
            grp_scripts = grp.get("script_urls") or []
            grp_authors = grp.get("script_authors") or {}

            if grp_name != "Main":
                pair.alt_group_config[grp_name] = {
                    "inherit_multi_axis": bool(grp.get("inherit_multi_axis", True)),
                    "display_name": (grp.get("display_name") or "").strip(),
                }

            for url in grp_videos:
                provider = detect_provider(url)
                if self._is_bundle_url(url):
                    item = PairItem(
                        url=url,
                        filename="(bundle - will resolve)",
                        file_type=FileType.VIDEO,
                        provider_name=provider,
                        is_bundle=True,
                        group=grp_name,
                    )
                else:
                    filename = self._guess_filename(url, "video")
                    item = PairItem(
                        url=url,
                        filename=filename,
                        file_type=FileType.VIDEO,
                        provider_name=provider,
                        group=grp_name,
                    )
                pair.items.append(item)

            for url in grp_scripts:
                provider = detect_provider(url)
                filename = self._guess_filename(url, "funscript")
                item = PairItem(
                    url=url,
                    filename=filename,
                    file_type=FileType.FUNSCRIPT,
                    provider_name=provider,
                    author=grp_authors.get(url, ""),
                    group=grp_name,
                )
                pair.items.append(item)

        self.pairs.append(pair)
        logger.info("Added pair: %s (%d items)", name, len(pair.items))

        if self.on_pair_added:
            self.on_pair_added(pair)
        if self.on_queue_changed:
            self.on_queue_changed()

        # Ensure pump is alive — restart if it died silently
        self._ensure_pump_alive()
        # Wake pump immediately so it picks up the new pair
        self._wake_pump()

        return pair

    def _wake_pump(self) -> None:
        """Thread-safe pump wake — can be called from any thread."""
        if self._dl_loop and self._dl_loop.is_running():
            self._dl_loop.call_soon_threadsafe(self._pump_wake.set)
        else:
            self._pump_wake.set()

    @staticmethod
    def _is_bundle_url(url: str) -> bool:
        """Check if URL is a Pixeldrain list or MEGA folder (may contain multiple files)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")

        # Pixeldrain list: /l/{list_id}
        if "pixeldrain.com" in host and path.startswith("l/"):
            return True

        # MEGA folder: /folder/ in URL
        if ("mega.nz" in host or "mega.co.nz" in host) and "/folder/" in url:
            return True

        return False

    def remove_pair(self, pair_id: str) -> None:
        self.pairs = [p for p in self.pairs if p.id != pair_id]
        if self.on_queue_changed:
            self.on_queue_changed()
        if self.on_save_needed:
            self.on_save_needed()

    def move_pair(self, pair_id: str, direction: int) -> None:
        """Move pair up (-1) or down (+1) in queue."""
        for i, pair in enumerate(self.pairs):
            if pair.id == pair_id:
                new_idx = i + direction
                if 0 <= new_idx < len(self.pairs):
                    self.pairs[i], self.pairs[new_idx] = self.pairs[new_idx], self.pairs[i]
                break
        if self.on_queue_changed:
            self.on_queue_changed()
        if self.on_save_needed:
            self.on_save_needed()

    def pause_pair(self, pair_id: str) -> None:
        pair = self._find_pair(pair_id)
        if not pair:
            return
        item_ids = self._get_item_ids(pair_id)

        def _do_pause():
            # Pause DownloadTask-based downloads (segment-based, supports resume)
            for dt in self._download_tasks:
                if dt.item.id in item_ids:
                    dt.pause()
            # Cancel ALL asyncio tasks for this pair (MEGA, HLS, etc.)
            # They don't support pause, but cancelling is safe — they
            # resume from disk on the next attempt.
            for task in self._pair_tasks.get(pair_id, []):
                if not task.done():
                    task.cancel()

        if self._dl_loop and self._dl_loop.is_running():
            self._dl_loop.call_soon_threadsafe(_do_pause)
        else:
            _do_pause()

        pair.state = PairState.PAUSED
        for item in pair.items:
            if item.state in (ItemState.DOWNLOADING, ItemState.RESOLVING):
                item.state = ItemState.PAUSED
        if self.on_pair_updated:
            self.on_pair_updated(pair)

    def resume_pair(self, pair_id: str) -> None:
        pair = self._find_pair(pair_id)
        if not pair:
            return
        if pair.state == PairState.PAUSED:
            item_ids = self._get_item_ids(pair_id)

            def _do_resume():
                for dt in self._download_tasks:
                    if dt.item.id in item_ids:
                        dt.resume()

            if self._dl_loop and self._dl_loop.is_running():
                self._dl_loop.call_soon_threadsafe(_do_resume)
            else:
                _do_resume()

            pair.state = PairState.DOWNLOADING
            if self.on_pair_updated:
                self.on_pair_updated(pair)
        elif pair.state == PairState.FAILED:
            # Re-queue failed pair
            pair.state = PairState.QUEUED
            for item in pair.items:
                if item.state == ItemState.FAILED:
                    item.state = ItemState.PENDING
            if self.on_pair_updated:
                self.on_pair_updated(pair)
            self._wake_pump()

    def organize_pair(self, pair_id: str) -> bool:
        """Manually trigger file organize (rename) for a completed pair."""
        pair = self._find_pair(pair_id)
        if not pair or pair.state != PairState.COMPLETED:
            return False
        if pair.organized:
            return False
        self._organize_output(pair)
        if self.on_pair_updated:
            self.on_pair_updated(pair)
        if self.on_save_needed:
            self.on_save_needed()
        return True

    def undo_organize_pair(self, pair_id: str) -> bool:
        """Undo file organize for a completed pair, restoring original filenames."""
        pair = self._find_pair(pair_id)
        if not pair or pair.state != PairState.COMPLETED:
            return False
        if not pair.organized:
            return False
        self._undo_organize(pair)
        if self.on_pair_updated:
            self.on_pair_updated(pair)
        if self.on_save_needed:
            self.on_save_needed()
        return True

    async def _pump(self) -> None:
        """Main loop: pick queued pairs and download them (up to max_concurrent_pairs)."""
        import time
        from funpairdl.persistence.settings import Settings

        logger.info("Pump started. Queue has %d pairs.", len(self.pairs))
        self._pump_heartbeat = time.monotonic()

        # On startup, reset any pairs stuck in DOWNLOADING (from a previous crash/exit)
        for pair in self.pairs:
            if pair.state == PairState.DOWNLOADING:
                logger.warning("Resetting stale DOWNLOADING pair to QUEUED: %s", pair.name)
                pair.state = PairState.QUEUED
                for item in pair.items:
                    if item.state in (ItemState.RESOLVING, ItemState.DOWNLOADING):
                        item.state = ItemState.PENDING
                if self.on_pair_updated:
                    self.on_pair_updated(pair)

        # Track actively downloading pair tasks: {pair.id: asyncio.Task}
        active_pair_tasks: dict[str, asyncio.Task] = {}

        last_idle_log = time.monotonic()
        while self._running:
            self._pump_heartbeat = time.monotonic()

            # Clean up finished tasks
            done_ids = [
                pid for pid, task in active_pair_tasks.items() if task.done()
            ]
            for pid in done_ids:
                task = active_pair_tasks.pop(pid)
                # Propagate exceptions (logging only — pair state already set)
                if task.exception() and not isinstance(task.exception(), asyncio.CancelledError):
                    logger.error("Pair task exception: %s", task.exception())

            settings = Settings.load()
            max_concurrent = max(1, settings.max_concurrent_pairs)
            slots_available = max_concurrent - len(active_pair_tasks)

            # Fill available slots with queued pairs
            launched = 0
            while slots_available > 0:
                next_pair = self._next_queued_pair()
                if next_pair is None:
                    break

                # Mark DOWNLOADING *before* create_task so the same pair
                # is never picked twice when max_concurrent_pairs > 1.
                # (create_task doesn't yield — the inner while loop runs
                #  synchronously, and _next_queued_pair would return the
                #  same still-QUEUED pair on the next iteration.)
                next_pair.state = PairState.DOWNLOADING
                if self.on_pair_updated:
                    self.on_pair_updated(next_pair)

                logger.info(
                    "Pump: starting pair '%s' (%d items) [%d/%d active]",
                    next_pair.name, len(next_pair.items),
                    len(active_pair_tasks) + 1, max_concurrent,
                )
                task = asyncio.create_task(self._run_pair_safe(next_pair))
                active_pair_tasks[next_pair.id] = task
                slots_available -= 1
                launched += 1

            if not active_pair_tasks:
                now = time.monotonic()
                if now - last_idle_log >= 60:
                    queued = sum(1 for p in self.pairs if p.state == PairState.QUEUED)
                    logger.info("Pump alive (idle). %d pairs total, %d queued.", len(self.pairs), queued)
                    last_idle_log = now

            # Wait for wake signal or timeout
            self._pump_wake.clear()
            try:
                await asyncio.wait_for(self._pump_wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _run_pair_safe(self, pair: Pair) -> None:
        """Download a pair with error handling. Used as a concurrent task."""
        try:
            await self._download_pair(pair)
        except Exception as e:
            logger.error("Pump: pair '%s' crashed: %s", pair.name, e, exc_info=True)
            pair.state = PairState.FAILED
            for item in pair.items:
                if item.state not in (ItemState.COMPLETED, ItemState.FAILED):
                    item.state = ItemState.FAILED
                    item.error_message = f"Pump crash: {e}"
            if self.on_pair_updated:
                self.on_pair_updated(pair)
            if self.on_save_needed:
                self.on_save_needed()

    async def _resolve_bundles(self, pair: Pair) -> bool:
        """Resolve bundle URLs (Pixeldrain lists, MEGA folders) into individual files.
        Replaces the bundle placeholder item with actual video/script items.

        Returns True if any bundles were expanded into multiple files."""
        from funpairdl.providers.pixeldrain import PixeldrainProvider
        from funpairdl.persistence.settings import Settings

        bundle_items = [i for i in pair.items if i.is_bundle]
        if not bundle_items:
            return False

        settings = Settings.load()
        new_items = []

        for bundle_item in bundle_items:
            try:
                url = bundle_item.url
                provider = bundle_item.provider_name
                resolved_files = []

                if provider == "pixeldrain":
                    pd = PixeldrainProvider(api_key=settings.pixeldrain_api_key)
                    resolved_files = await pd.resolve_list_all(url)
                elif provider == "mega":
                    from funpairdl.utils.mega_api import probe_mega_folder
                    result = await probe_mega_folder(url)
                    if result.get("success") and result.get("files"):
                        for f in result["files"]:
                            fname = sanitize_filename(f.get("name", "mega_file"))
                            resolved_files.append(ResolvedFile(
                                direct_url=f["url"],
                                filename=fname,
                                total_size=f.get("size", 0),
                                supports_range=False,
                                is_mega=True,
                                mega_url=f["url"],
                            ))
                    else:
                        # Fallback: treat as single item
                        bundle_item.is_bundle = False
                        bundle_item.filename = self._guess_filename(url, "video")
                        continue
                else:
                    bundle_item.is_bundle = False
                    continue

                if not resolved_files:
                    logger.warning("Bundle resolved to 0 files: %s", url)
                    bundle_item.is_bundle = False
                    continue

                # Categorize each file by extension
                for rf in resolved_files:
                    fname = rf.filename.lower()
                    if fname.endswith(".funscript"):
                        file_type = FileType.FUNSCRIPT
                    elif any(fname.endswith(ext) for ext in [".mp4", ".mkv", ".avi", ".webm", ".mov", ".wmv", ".m4v"]):
                        file_type = FileType.VIDEO
                    else:
                        file_type = FileType.OTHER

                    new_item = PairItem(
                        url=rf.direct_url,
                        filename=rf.filename,
                        file_type=file_type,
                        provider_name=provider,
                        total_bytes=rf.total_size,
                        headers=rf.headers or {},
                        resolved_url=rf.direct_url,
                    )
                    new_items.append(new_item)

                logger.info(
                    "Bundle %s resolved to %d files (%d video, %d script, %d other)",
                    url, len(resolved_files),
                    sum(1 for i in new_items if i.file_type == FileType.VIDEO),
                    sum(1 for i in new_items if i.file_type == FileType.FUNSCRIPT),
                    sum(1 for i in new_items if i.file_type == FileType.OTHER),
                )

            except Exception as e:
                logger.error("Failed to resolve bundle %s: %s", bundle_item.url, e)
                bundle_item.is_bundle = False
                bundle_item.filename = self._guess_filename(bundle_item.url, "video")

        # Replace bundle items with resolved items
        expanded = False
        if new_items:
            pair.items = [i for i in pair.items if not i.is_bundle] + new_items
            expanded = True

        if self.on_pair_updated:
            self.on_pair_updated(pair)

        return expanded

    def _get_registry(self) -> ProviderRegistry:
        if self._registry is None:
            from funpairdl.persistence.settings import Settings
            settings = Settings.load()
            self._registry = ProviderRegistry(
                pixeldrain_api_key=settings.pixeldrain_api_key,
                mega_email=settings.mega_email,
                mega_password=settings.mega_password,
                gofile_token=settings.gofile_token,
            )
        return self._registry

    async def _resolve_item(self, item: PairItem, preferred_resolution: str = "best") -> ResolvedFile | None:
        """Resolve a PairItem's URL through the provider system."""
        try:
            item.state = ItemState.RESOLVING
            if self.on_item_updated:
                self.on_item_updated(item)

            registry = self._get_registry()
            from funpairdl.persistence.settings import Settings
            settings = Settings.load()

            try:
                resolved = await asyncio.wait_for(
                    registry.resolve(
                        item.url,
                        cookies_from_browser=settings.cookies_from_browser,
                        preferred_resolution=preferred_resolution,
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Resolve timed out after 60s: {item.url[:80]}")

            item.resolved_url = resolved.direct_url
            if resolved.headers:
                item.headers = resolved.headers
            if resolved.total_size:
                item.total_bytes = resolved.total_size
            if resolved.filename:
                item.filename = resolved.filename

            logger.info(
                "Resolved %s -> %s (%s, %d bytes)",
                item.url[:60], item.resolved_url[:60],
                item.provider_name, item.total_bytes,
            )
            return resolved

        except Exception as e:
            logger.error("Failed to resolve %s: %s", item.url, e)
            item.state = ItemState.FAILED
            item.error_message = f"Resolve failed: {e}"
            if self.on_item_updated:
                self.on_item_updated(item)
            return None

    async def _download_mega(self, item: PairItem, output_dir: Path) -> None:
        """Download a file from MEGA using built-in decryption (no mega.py)."""
        try:
            # Skip if the output file already exists with the expected size
            if item.total_bytes > 0:
                final = output_dir / item.filename
                if final.exists() and final.stat().st_size >= item.total_bytes:
                    item.downloaded_bytes = item.total_bytes
                    item.state = ItemState.COMPLETED
                    if self.on_item_updated:
                        self.on_item_updated(item)
                    logger.info("MEGA skip (already on disk): %s", item.filename)
                    return

            item.state = ItemState.DOWNLOADING
            if self.on_item_updated:
                self.on_item_updated(item)

            from funpairdl.core.progress import SpeedCalculator
            from funpairdl.persistence.settings import Settings
            from funpairdl.utils.mega_api import download_mega_file, validate_mega_sid

            settings = Settings.load()

            # Validate SID before attempting download
            if settings.mega_sid:
                sid_info = await validate_mega_sid(settings.mega_sid)
                if sid_info["valid"]:
                    logger.info(
                        "MEGA session valid — account type: %s",
                        sid_info["type"],
                    )
                else:
                    logger.warning(
                        "MEGA session invalid (%s) — falling back to anonymous. "
                        "Open mega.nz in embedded browser to refresh.",
                        sid_info["error"],
                    )
                    settings.mega_sid = ""  # Don't use expired sid
            else:
                logger.warning("No MEGA session ID configured — downloading anonymously")

            speed_calc = SpeedCalculator()
            speed_calc.reset()
            last_update = [0.0]

            def _on_progress(downloaded: int, total: int):
                import time
                item.downloaded_bytes = downloaded
                item.total_bytes = total
                now = time.monotonic()
                if now - last_update[0] < 0.2:
                    return
                last_update[0] = now
                speed_calc.update(downloaded)
                item.speed_bps = speed_calc.speed_bps
                if self.on_item_updated:
                    self.on_item_updated(item)

            result_path = await download_mega_file(
                item.url, output_dir, on_progress=_on_progress,
                sid=settings.mega_sid,
                max_segments=settings.max_segments,
            )

            item.filename = result_path.name
            item.downloaded_bytes = result_path.stat().st_size
            item.total_bytes = item.downloaded_bytes
            item.speed_bps = 0
            item.state = ItemState.COMPLETED
            if self.on_item_updated:
                self.on_item_updated(item)

            logger.info("MEGA download complete: %s", item.filename)

        except Exception as e:
            logger.error("MEGA download failed for %s: %s", item.url, e)
            item.state = ItemState.FAILED
            item.error_message = str(e)
            if self.on_item_updated:
                self.on_item_updated(item)
            raise

    async def _download_hls(self, item: PairItem, output_dir: Path, preferred_resolution: str = "best") -> None:
        """Download an HLS stream using yt-dlp."""
        try:
            # Skip if the output file already exists with the expected size
            if item.total_bytes > 0:
                final = output_dir / item.filename
                if final.exists() and final.stat().st_size >= item.total_bytes:
                    item.downloaded_bytes = item.total_bytes
                    item.state = ItemState.COMPLETED
                    if self.on_item_updated:
                        self.on_item_updated(item)
                    logger.info("HLS skip (already on disk): %s", item.filename)
                    return

            item.state = ItemState.DOWNLOADING
            if self.on_item_updated:
                self.on_item_updated(item)

            from funpairdl.persistence.settings import Settings
            settings = Settings.load()

            original_url = item.url  # Use original page URL for yt-dlp

            def _download():
                import yt_dlp
                output_dir.mkdir(parents=True, exist_ok=True)
                base = item.filename.rsplit(".", 1)[0] if "." in item.filename else item.filename
                output_template = str(output_dir / base) + ".%(ext)s"

                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "outtmpl": output_template,
                }
                # Use impersonation to bypass Cloudflare (requires curl_cffi)
                try:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    ydl_opts["impersonate"] = ImpersonateTarget(client="chrome")
                except ImportError:
                    pass

                # Apply resolution preference: exact match or best
                if preferred_resolution and preferred_resolution != "best":
                    try:
                        h = int(preferred_resolution)
                        ydl_opts["format"] = (
                            f"bestvideo[height={h}]+bestaudio/best[height={h}]/best"
                        )
                    except ValueError:
                        pass

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(original_url, download=True)
                    return info

            try:
                info = await asyncio.wait_for(asyncio.to_thread(_download), timeout=600)
            except asyncio.TimeoutError:
                raise TimeoutError(f"HLS download timed out after 600s: {item.url[:80]}")

            if info:
                ext = info.get("ext", "mp4")
                base = item.filename.rsplit(".", 1)[0] if "." in item.filename else item.filename
                item.filename = f"{base}.{ext}"
                filesize = info.get("filesize") or info.get("filesize_approx") or 0
                if filesize:
                    item.total_bytes = filesize
                    item.downloaded_bytes = filesize
                else:
                    # Try to get actual file size — yt-dlp may have used
                    # a different extension after remuxing (e.g. webm → mp4)
                    final_path = output_dir / item.filename
                    if not final_path.exists():
                        matches = list(output_dir.glob(f"{base}.*"))
                        # Exclude temp/part files
                        matches = [m for m in matches if not m.suffix.endswith(".part")]
                        if matches:
                            final_path = matches[0]
                            item.filename = final_path.name
                    if final_path.exists():
                        item.total_bytes = final_path.stat().st_size
                        item.downloaded_bytes = item.total_bytes

            # Always sync bytes — if we still have no size info, mark as
            # zero so progress doesn't show a misleading stale percentage.
            if item.downloaded_bytes != item.total_bytes:
                if item.total_bytes > 0:
                    item.downloaded_bytes = item.total_bytes
                else:
                    item.total_bytes = 0
                    item.downloaded_bytes = 0

            item.speed_bps = 0
            item.state = ItemState.COMPLETED
            if self.on_item_updated:
                self.on_item_updated(item)

            logger.info("HLS download complete: %s", item.filename)

        except Exception as e:
            logger.error("HLS download failed for %s: %s", item.url, e)
            item.state = ItemState.FAILED
            item.error_message = str(e)
            if self.on_item_updated:
                self.on_item_updated(item)
            raise

    def _next_queued_pair(self) -> Pair | None:
        for pair in self.pairs:
            if pair.state == PairState.QUEUED:
                return pair
        return None

    async def _download_pair(self, pair: Pair) -> None:
        pair.state = PairState.DOWNLOADING
        if self.on_pair_updated:
            self.on_pair_updated(pair)

        # Resolve bundle URLs before downloading
        had_bundles = await self._resolve_bundles(pair)

        # Auto-split only after bundle expansion (not for user-picked video mirrors)
        new_pairs = self._auto_split_bundle_pair(pair) if had_bundles else None
        if new_pairs:
            for np in new_pairs:
                self.pairs.append(np)
                logger.info("Auto-split: created pair '%s' (%d items)", np.name, len(np.items))
                if self.on_pair_added:
                    self.on_pair_added(np)
            pair.state = PairState.COMPLETED
            pair.items.clear()
            logger.info("Auto-split: original pair '%s' split into %d pairs", pair.name, len(new_pairs))
            if self.on_pair_updated:
                self.on_pair_updated(pair)
            if self.on_queue_changed:
                self.on_queue_changed()
            return

        output_dir = Path(pair.output_dir)

        # Track this pair's tasks locally; also register in shared lists for pause/cancel
        pair_download_tasks: list[DownloadTask] = []
        pair_current_tasks: list[asyncio.Task] = []
        self._pair_tasks[pair.id] = pair_current_tasks  # expose for pause/cancel

        # Phase 0: Mark items whose output files are already on disk as COMPLETED.
        # Must run BEFORE resolve — resolve may change item.filename, making
        # the file undetectable.
        for item in pair.items:
            if item.state == ItemState.COMPLETED:
                continue
            if item.total_bytes > 0:
                final = output_dir / item.filename
                if final.exists() and final.stat().st_size >= item.total_bytes:
                    item.downloaded_bytes = item.total_bytes
                    item.state = ItemState.COMPLETED
                    item.error_message = ""
                    logger.info("Already on disk (skip): %s", item.filename)
                    if self.on_item_updated:
                        self.on_item_updated(item)

        # Phase 1: Resolve all items through provider system (concurrently)
        resolved_info: dict[str, ResolvedFile] = {}
        items_to_resolve = [i for i in pair.items if i.state not in (ItemState.COMPLETED,)]

        async def _resolve_one(item):
            resolved = await self._resolve_item(item, pair.preferred_resolution)
            if resolved:
                resolved_info[item.id] = resolved

        await asyncio.gather(
            *[_resolve_one(i) for i in items_to_resolve],
            return_exceptions=True,
        )

        if self.on_pair_updated:
            self.on_pair_updated(pair)

        # Phase 1.5: Recover items whose segments are already complete on disk.
        # After a crash, duplicate-launch, or interrupted merge, segment temp
        # files may be fully downloaded even though the item is not COMPLETED.
        # Merge them directly instead of re-downloading.
        await self._recover_complete_items(pair, output_dir)

        # Phase 2: Download items with item-level retry.
        # If items fail, re-resolve (CDN URLs may expire) and retry up to
        # MAX_ITEM_RETRIES times before giving up.
        MAX_ITEM_RETRIES = 2
        mega_sem = asyncio.Semaphore(4)  # Limit concurrent MEGA downloads

        async def _mega_with_limit(item, out_dir):
            async with mega_sem:
                return await self._download_mega(item, out_dir)

        for attempt in range(MAX_ITEM_RETRIES + 1):
            # Collect items that still need downloading
            items_pending = [
                i for i in pair.items
                if i.state not in (ItemState.COMPLETED, ItemState.PAUSED)
            ]
            if not items_pending:
                break

            # On retry rounds, re-resolve failed items to get fresh URLs
            if attempt > 0:
                failed_items = [i for i in items_pending if i.state == ItemState.FAILED]
                if not failed_items:
                    break

                delay = 5 * (2 ** (attempt - 1))
                logger.info(
                    "Retrying %d failed item(s) in %ds (attempt %d/%d): %s",
                    len(failed_items), delay, attempt + 1, MAX_ITEM_RETRIES + 1,
                    pair.name,
                )
                await asyncio.sleep(delay)

                if pair.state == PairState.PAUSED:
                    break

                for item in failed_items:
                    # Clean up partial segment files from the failed attempt
                    for seg in item.segments:
                        try:
                            Path(seg.temp_file).unlink(missing_ok=True)
                        except OSError:
                            pass
                    item.segments.clear()
                    item.state = ItemState.PENDING
                    item.error_message = ""
                    item.resolved_url = ""
                    item.downloaded_bytes = 0
                    item.total_bytes = 0

                # Re-resolve
                async def _re_resolve(it):
                    resolved = await self._resolve_item(it, pair.preferred_resolution)
                    if resolved:
                        resolved_info[it.id] = resolved

                await asyncio.gather(
                    *[_re_resolve(i) for i in failed_items if i.state == ItemState.PENDING],
                    return_exceptions=True,
                )
                if self.on_pair_updated:
                    self.on_pair_updated(pair)

            # Create download tasks for pending items
            tasks = []
            for item in pair.items:
                if item.state in (ItemState.COMPLETED, ItemState.FAILED):
                    continue

                resolved = resolved_info.get(item.id)
                if not resolved:
                    continue

                if resolved.is_mega:
                    task = asyncio.create_task(_mega_with_limit(item, output_dir))
                elif resolved.is_hls:
                    task = asyncio.create_task(self._download_hls(item, output_dir, pair.preferred_resolution))
                else:
                    segments = self.num_segments
                    if item.provider_name == "gofile":
                        segments = min(segments, 4)

                    dt = DownloadTask(
                        item=item,
                        output_dir=output_dir,
                        num_segments=segments,
                        on_progress=self._on_item_progress,
                        on_state_change=self._on_item_state_change,
                    )
                    pair_download_tasks.append(dt)
                    self._download_tasks.append(dt)
                    task = asyncio.create_task(self._run_download_task(dt))

                tasks.append(task)
                pair_current_tasks.append(task)
                self._current_tasks.append(task)

            if not tasks:
                # No downloadable items this round — but there may be failed
                # items worth retrying on the next attempt (e.g. resolve failures).
                if attempt < MAX_ITEM_RETRIES and any(
                    i.state == ItemState.FAILED for i in pair.items
                ):
                    continue
                break

            # Wait for all to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.error("Download task exception: %s", result)

        # Force-sync bytes for completed items: ensure downloaded_bytes ==
        # total_bytes so progress never shows a stale mid-download percentage.
        for item in pair.items:
            if item.state == ItemState.COMPLETED and item.total_bytes > 0:
                item.downloaded_bytes = item.total_bytes

        # Catch items stuck in transient states after retry loop exits.
        # This should not happen, but guard against it so the pair state
        # determination below is reliable.
        for item in pair.items:
            if item.state in (ItemState.PENDING, ItemState.RESOLVING, ItemState.DOWNLOADING):
                logger.warning(
                    "Item '%s' stuck in %s after retry loop — marking FAILED",
                    item.filename, item.state.value,
                )
                item.state = ItemState.FAILED
                item.error_message = item.error_message or "Stuck in transient state"

        # Clean up .parts dir now that all items are done
        parts_dir = output_dir / ".parts"
        try:
            if parts_dir.exists() and not any(parts_dir.iterdir()):
                parts_dir.rmdir()
        except OSError:
            pass

        # Determine pair state from item states (source of truth).
        any_paused = any(i.state == ItemState.PAUSED for i in pair.items)
        all_completed = all(i.state == ItemState.COMPLETED for i in pair.items)

        if any_paused and pair.state == PairState.PAUSED:
            pass  # stay paused
        elif all_completed:
            # Unify filenames and organize variant subfolders (in thread to avoid blocking GUI)
            if pair.auto_rename:
                await asyncio.to_thread(self._organize_output, pair)
            pair.state = PairState.COMPLETED
            logger.info("Pair completed: %s", pair.name)
        elif not any_paused:
            pair.state = PairState.FAILED
            failed_items = [i for i in pair.items if i.state == ItemState.FAILED]
            logger.error(
                "Pair failed: %s (%d/%d items failed: %s)",
                pair.name, len(failed_items), len(pair.items),
                ", ".join(f"{i.filename}: {i.error_message}" for i in failed_items),
            )

        if self.on_pair_updated:
            self.on_pair_updated(pair)

        # Persist queue immediately when a pair finishes
        if pair.state in (PairState.COMPLETED, PairState.FAILED):
            if self.on_save_needed:
                self.on_save_needed()

        # Remove this pair's tasks from shared lists
        for dt in pair_download_tasks:
            try:
                self._download_tasks.remove(dt)
            except ValueError:
                pass
        for t in pair_current_tasks:
            try:
                self._current_tasks.remove(t)
            except ValueError:
                pass
        self._pair_tasks.pop(pair.id, None)

    async def _run_download_task(self, dt: DownloadTask) -> None:
        try:
            await dt.download(self._session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Download task error: %s", e)
            raise

    @staticmethod
    def _clean_title(title: str) -> str:
        """Clean article title: remove common prefixes/tags that aren't part of the name."""
        import re

        cleaned = title.strip()

        # Remove common bracket-wrapped tags at the start or end
        # e.g. (multi-axis), [Multi-Axis], (CS-FREE-0118), [Giddora], [cos], etc.
        # Strategy: remove tags like (multi-axis), (Multi Axis), (free), (paid) etc.
        # But keep author names and actual title content
        noise_patterns = [
            r'[\(（]\s*multi[- ]?axis\s*[\)）]',
            r'[\(（]\s*single[- ]?axis\s*[\)）]',
            r'[\(（]\s*free\s*[\)）]',
            r'[\(（]\s*paid\s*[\)）]',
            r'[\(（]\s*requested\s*[\)）]',
        ]
        for pat in noise_patterns:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)

        # Clean up leftover whitespace
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        # Remove leading/trailing dashes or hyphens left over
        cleaned = cleaned.strip('- –—').strip()

        return cleaned or title.strip()

    # Erodeck-recognized axis suffixes → canonical axis ID
    _ERODECK_AXIS_MAP: dict[str, str] = {
        "stroke": "L0", "l0": "L0",
        "surge": "L1", "l1": "L1",
        "sway": "L2", "l2": "L2",
        "suck": "L3", "l3": "L3",
        "twist": "R0", "r0": "R0",
        "roll": "R1", "r1": "R1",
        "pitch": "R2", "r2": "R2",
        "vibe": "V0", "vibration": "V0", "vib": "V0", "v0": "V0",
        "pump": "V1", "lube": "V1", "v1": "V1",
        "valve": "V2", "v2": "V2",
        "a0": "A0", "a1": "A1", "a2": "A2",
    }

    @classmethod
    def _parse_axis(cls, filename: str) -> tuple[str, str]:
        """Parse funscript filename to extract (canonical_axis, display_suffix).

        Handles compound suffixes like '.L0.max.funscript':
        - Scans all dot-components between base name and .funscript
        - If a known erodeck axis is found → canonical = that axis
        - Otherwise → canonical = "L0" (main)
        - display_suffix = the known axis component (for output naming)

        Returns:
            (canonical_axis, display_suffix)
            canonical_axis: erodeck axis ID like "L0", "R2", etc.
            display_suffix: axis suffix to use in output filename, or "" for main
        """
        # Strip .funscript to get components
        stem = filename
        if stem.lower().endswith(".funscript"):
            stem = stem[:-len(".funscript")]

        parts = stem.split(".")
        # Scan components from right to left for a known axis
        for part in reversed(parts):
            canonical = cls._ERODECK_AXIS_MAP.get(part.lower())
            if canonical:
                return canonical, part
        # No known axis found → main axis (L0)
        return "L0", ""

    def _auto_split_bundle_pair(self, pair: Pair) -> list[Pair] | None:
        """If a resolved bundle produced multiple videos, split into separate
        pairs by matching each video to its scripts via filename stem.

        Returns new pairs if split occurred, or None if no split needed.
        """
        videos = [i for i in pair.items if i.file_type == FileType.VIDEO]
        if len(videos) <= 1:
            return None

        scripts = [i for i in pair.items if i.file_type == FileType.FUNSCRIPT]
        others = [i for i in pair.items if i.file_type == FileType.OTHER]

        # Build video stems sorted by length (longest first for greedy matching)
        video_stems: list[tuple[str, PairItem]] = []
        for v in videos:
            stem = Path(v.filename).stem
            video_stems.append((stem, v))
        video_stems.sort(key=lambda x: len(x[0]), reverse=True)

        # Match scripts to videos by stem prefix
        matched: dict[str, list[PairItem]] = {stem: [] for stem, _ in video_stems}
        unmatched_scripts: list[PairItem] = []

        for s in scripts:
            script_base = s.filename
            if script_base.lower().endswith(".funscript"):
                script_base = script_base[: -len(".funscript")]
            # Strip known axis suffix (e.g., ".pitch", ".roll")
            parts = script_base.rsplit(".", 1)
            if len(parts) == 2 and parts[1].lower() in self._ERODECK_AXIS_MAP:
                script_base = parts[0]

            best_stem = None
            for stem, _ in video_stems:
                if script_base == stem or script_base.startswith(stem):
                    best_stem = stem
                    break  # Already sorted longest-first, first match is best
            if best_stem:
                matched[best_stem].append(s)
            else:
                unmatched_scripts.append(s)

        # Create new pairs — each split pair becomes its own folder, so
        # whatever group label items carried from the bundle source is no
        # longer meaningful; reset to Main so organize treats them flatly.
        new_pairs: list[Pair] = []
        for stem, video_item in video_stems:
            name = sanitize_filename(self._clean_title(stem))
            new_pair = Pair(name=name, preferred_resolution=pair.preferred_resolution)
            new_pair.output_dir = str(self.download_dir / name)
            new_pair.items = [video_item] + matched.get(stem, [])
            for it in new_pair.items:
                it.group = "Main"
            new_pairs.append(new_pair)

        # Distribute unmatched scripts to all pairs (likely shared/generic)
        if unmatched_scripts:
            for s in unmatched_scripts:
                new_pairs[0].items.append(s)

        # Distribute "other" files to first pair
        if others:
            new_pairs[0].items.extend(others)

        logger.info(
            "Auto-split bundle pair '%s' (%d videos) into %d pairs: %s",
            pair.name, len(videos), len(new_pairs),
            ", ".join(f"'{p.name}' ({len(p.items)})" for p in new_pairs),
        )
        return new_pairs

    @staticmethod
    def _alt_slot_suffix(idx: int) -> str:
        """Map a zero-based Alt slot to its on-disk suffix.

        Convention: first Alt → ".alt", subsequent → ".alt1", ".alt2", ...
        Matches erodeck's expected layout and stays backward-compatible
        with the old author-collision code path.
        """
        return "alt" if idx == 0 else f"alt{idx}"

    @staticmethod
    def _alt_sort_key(name: str) -> tuple[int, str]:
        """Stable ordering for Alt group names.

        "Alt 1", "Alt 2" → sort by numeric suffix; anything else falls
        back to lexicographic. Lets "Alt 10" come after "Alt 2".
        """
        import re
        m = re.match(r"\s*Alt\s+(\d+)\s*$", name, re.IGNORECASE)
        if m:
            return (0, m.group(1).zfill(6))
        return (1, name)

    def _autopromote_main_collisions(self, pair: Pair) -> None:
        """Backward-compat helper: when Main contains axis collisions or
        (subfolder mode) multiple authors, auto-split the extras into
        implicit Alt groups so legacy flat-list submissions keep
        producing the same `.alt` layout.

        These auto-generated Alt groups use `inherit_multi_axis=False`
        because the historical behavior only hardlinked the Main video
        into alt folders — it never duplicated non-L0 funscripts. New
        explicitly-grouped pairs (from the picker UI) opt in to axis
        inheritance instead.
        """
        from collections import OrderedDict
        from funpairdl.persistence.settings import Settings

        main_items = [it for it in pair.items if (it.group or "Main") == "Main"]
        main_scripts = [it for it in main_items if it.file_type == FileType.FUNSCRIPT]
        if len(main_scripts) <= 1:
            return

        # Axis collision: two+ Main scripts share a canonical axis.
        axis_seen: OrderedDict[str, list[PairItem]] = OrderedDict()
        for item in main_scripts:
            canonical, _ = self._parse_axis(item.filename)
            axis_seen.setdefault(canonical, []).append(item)
        extras: list[PairItem] = []
        for _canonical, group in axis_seen.items():
            if len(group) > 1:
                extras.extend(group[1:])

        # Subfolder mode fallback: no axis collision but multiple authors.
        if not extras:
            try:
                variant_mode = Settings.load().script_variant_mode
            except Exception:
                variant_mode = "flat"
            if variant_mode != "subfolder":
                return
            authors_seen: OrderedDict[str, list[PairItem]] = OrderedDict()
            for item in main_scripts:
                authors_seen.setdefault(item.author or "", []).append(item)
            if len(authors_seen) <= 1:
                return
            for ai, (_author, items) in enumerate(authors_seen.items()):
                if ai == 0:
                    continue
                extras.extend(items)

        if not extras:
            return

        # Bucket extras by author so each scripter gets its own slot
        # (matches the old author-grouped author-_groups iteration).
        next_n = 1
        used = set(pair.alt_group_config.keys()) | {it.group for it in pair.items if it.group}
        while f"Alt {next_n}" in used:
            next_n += 1

        by_author: OrderedDict[str, list[PairItem]] = OrderedDict()
        for it in extras:
            key = it.author or f"_anon_{id(it)}"
            by_author.setdefault(key, []).append(it)

        for _author, items in by_author.items():
            alt_name = f"Alt {next_n}"
            next_n += 1
            pair.alt_group_config.setdefault(alt_name, {"inherit_multi_axis": False})
            for it in items:
                it.group = alt_name

    def _organize_output(self, pair: Pair) -> None:
        """Rename files to share the same base name and place each Alt
        group into its own erodeck-compatible subfolder.

        Grouping is driven by `item.group`:
          - "Main" / "" → root folder, renamed to `<base>.<axis>.funscript`
          - "Alt N"     → `.alt[N-1]/` subfolder, renamed to
                          `<base>.alt[N-1].<axis>.funscript`

        If an Alt group has no video of its own, Main's video is
        hardlinked into the subfolder (legacy "alternate scripter for
        the same video" behavior). If the group's config sets
        `inherit_multi_axis=True` (default), Main's non-L0 funscripts
        are hardlinked into the subfolder for any axis the Alt itself
        doesn't already cover.
        """
        import os
        from collections import OrderedDict
        from datetime import date

        output_dir = Path(pair.output_dir)
        base_name = sanitize_filename(self._clean_title(pair.name))

        # Save original filenames before renaming (for undo)
        if not pair.original_filenames:
            pair.original_filenames = {item.id: item.filename for item in pair.items}

        # Backward compat: auto-promote axis/author collisions inside
        # Main into implicit Alt groups (legacy flat-list submissions
        # relied on this).
        self._autopromote_main_collisions(pair)

        # ─── Partition items by group ───
        # Treat empty group (legacy queue) as "Main".
        group_items: OrderedDict[str, list[PairItem]] = OrderedDict()
        group_items["Main"] = []
        for item in pair.items:
            gname = item.group or "Main"
            if gname not in group_items:
                group_items[gname] = []
            group_items[gname].append(item)

        alt_names = sorted(
            [g for g in group_items if g != "Main"],
            key=self._alt_sort_key,
        )

        hardlinks: list[tuple[str, str]] = []

        # ─── Main group: rename in root ───
        main_items = group_items.get("Main", [])
        main_videos = [i for i in main_items if i.file_type == FileType.VIDEO]
        main_scripts = [i for i in main_items if i.file_type == FileType.FUNSCRIPT]

        video_ext = ".mp4"
        main_video_path: Path | None = None  # for sibling-hardlink + Alt fallback

        # Multiple Main videos = mirrors from different hosts. First wins
        # the rename; later ones keep their original name and get sibling
        # funscripts via the hardlink pass below.
        for item in main_videos:
            old_path = output_dir / item.filename
            if not old_path.exists():
                continue
            cur_ext = old_path.suffix
            if main_video_path is None:
                video_ext = cur_ext
                new_name = f"{base_name}{video_ext}"
                new_path = output_dir / new_name
                if old_path != new_path:
                    if new_path.exists():
                        logger.warning("Target file already exists, skipping rename: %s", new_path)
                        main_video_path = new_path  # treat the existing one as primary
                        continue
                    try:
                        old_path.rename(new_path)
                        item.filename = new_name
                        main_video_path = new_path
                        logger.info("Renamed: %s -> %s", old_path.name, new_name)
                    except OSError as e:
                        logger.error("Failed to rename %s: %s", old_path.name, e)
                else:
                    main_video_path = new_path

        # Rename Main scripts; track per-axis primary for inheritance.
        # If two Main scripts collide on the same axis, the first wins
        # the rename and we log a warning — moving such collisions into
        # a real Alt group is now the user's job via the picker UI.
        main_axis_primary: dict[str, tuple[PairItem, str]] = {}
        for item in main_scripts:
            canonical, suffix = self._parse_axis(item.filename)
            old_path = output_dir / item.filename
            if not old_path.exists():
                continue
            if canonical in main_axis_primary:
                logger.warning(
                    "Main has two scripts on axis %s: %s and %s — keeping second under original name",
                    canonical, main_axis_primary[canonical][0].filename, item.filename,
                )
                continue
            new_name = f"{base_name}.{suffix}.funscript" if suffix else f"{base_name}.funscript"
            new_path = output_dir / new_name
            if old_path != new_path:
                if new_path.exists():
                    logger.warning("Target exists, skipping: %s", new_path)
                    continue
                try:
                    old_path.rename(new_path)
                    item.filename = new_name
                    logger.info("Renamed: %s -> %s", old_path.name, new_name)
                except OSError as e:
                    logger.error("Failed to rename %s: %s", old_path.name, e)
                    continue
            main_axis_primary[canonical] = (item, suffix)

        # ─── Resolve each Alt group's on-disk stem ───
        # Priority: display_name (user-supplied via UI) → topic + slot
        # number. Same-name collisions get a `-2`, `-3` suffix so two
        # Alts that share a display label don't clobber each other.
        alt_bases: dict[str, str] = {}
        used_bases: set[str] = set()
        for slot_idx, alt_name in enumerate(alt_names):
            cfg = pair.alt_group_config.get(alt_name, {})
            disp = (cfg.get("display_name") or "").strip()
            if disp:
                stem = sanitize_filename(disp)
            else:
                stem = f"{base_name}.{self._alt_slot_suffix(slot_idx)}"
            if not stem:
                stem = f"{base_name}.{self._alt_slot_suffix(slot_idx)}"
            candidate = f"{stem}.alt" if disp else stem
            if candidate in used_bases:
                n = 2
                while f"{stem}-{n}.alt" in used_bases:
                    n += 1
                candidate = f"{stem}-{n}.alt"
            used_bases.add(candidate)
            alt_bases[alt_name] = candidate

        for slot_idx, alt_name in enumerate(alt_names):
            alt_base = alt_bases[alt_name]
            alt_dir = output_dir / alt_base
            alt_dir.mkdir(parents=True, exist_ok=True)

            alt_items = group_items[alt_name]
            alt_videos = [i for i in alt_items if i.file_type == FileType.VIDEO]
            alt_scripts = [i for i in alt_items if i.file_type == FileType.FUNSCRIPT]

            # Place Alt video. If the Alt group has its own video, move it
            # into the subfolder under the alt name. Otherwise hardlink
            # Main's video (preserves the "alternate scripter for the
            # same video" workflow).
            alt_video_placed = False
            if alt_videos:
                first = alt_videos[0]
                src = output_dir / first.filename
                if src.exists():
                    dest = alt_dir / f"{alt_base}{src.suffix}"
                    if not dest.exists():
                        try:
                            src.rename(dest)
                            first.filename = dest.name
                            alt_video_placed = True
                            logger.info("Moved Alt video: %s -> %s", src.name, dest)
                        except OSError as e:
                            logger.error("Failed to move Alt video %s: %s", src, e)
                # Any further videos in the same Alt group are mirrors —
                # leave them in root with their original names; the
                # sibling-funscript pass below will pair them up.

            if not alt_video_placed and main_video_path is not None and main_video_path.exists():
                dest = alt_dir / f"{alt_base}{main_video_path.suffix}"
                if not dest.exists():
                    try:
                        os.link(str(main_video_path), str(dest))
                        hardlinks.append((str(main_video_path), str(dest)))
                        logger.info("Hardlinked Main video into %s", alt_dir.name)
                    except OSError as e:
                        logger.error("Failed to hardlink Main video into %s: %s", alt_dir, e)

            # Move + rename Alt scripts; remember which axes the Alt
            # already covers so inheritance doesn't double-fill them.
            alt_axes_covered: set[str] = set()
            for item in alt_scripts:
                canonical, suffix = self._parse_axis(item.filename)
                src = output_dir / item.filename
                if not src.exists():
                    continue
                new_name = f"{alt_base}.{suffix}.funscript" if suffix else f"{alt_base}.funscript"
                dest = alt_dir / new_name
                if dest.exists():
                    logger.warning("Target exists, skipping: %s", dest)
                    continue
                try:
                    src.rename(dest)
                    item.filename = new_name
                    alt_axes_covered.add(canonical)
                    logger.info("Moved Alt script: %s -> %s", src.name, dest)
                except OSError as e:
                    logger.error("Failed to move %s: %s", src.name, e)

            # Inherit Main's multi-axis funscripts when configured.
            # We never inherit L0 (the main axis) — that's what each Alt
            # group's own primary funscript represents.
            cfg = pair.alt_group_config.get(alt_name, {})
            inherit = bool(cfg.get("inherit_multi_axis", True))
            if inherit:
                for canonical, (main_item, main_suffix) in main_axis_primary.items():
                    if canonical == "L0":
                        continue
                    if canonical in alt_axes_covered:
                        continue
                    main_path = output_dir / main_item.filename
                    if not main_path.exists():
                        continue
                    hl_name = f"{alt_base}.{main_suffix}.funscript"
                    hl_dest = alt_dir / hl_name
                    if hl_dest.exists():
                        continue
                    try:
                        os.link(str(main_path), str(hl_dest))
                        hardlinks.append((str(main_path), str(hl_dest)))
                        logger.info("Inherited Main %s axis into %s", canonical, alt_dir.name)
                    except OSError as e:
                        logger.warning("Failed to inherit Main %s into %s: %s", canonical, alt_dir, e)

        # ─── Sibling-funscript hardlinks for extra Main mirrors ───
        # When Main has multiple videos but one funscript, players only
        # see the script next to the matching stem. Hardlink the primary
        # Main funscript next to every Main mirror video.
        primary_funscript_path: Path | None = None
        primary_script = output_dir / f"{base_name}.funscript"
        if primary_script.exists():
            primary_funscript_path = primary_script

        if primary_funscript_path is not None:
            for item in main_videos:
                vid_path = output_dir / item.filename
                if not vid_path.exists():
                    continue
                expected_script = vid_path.with_suffix(".funscript")
                if expected_script.exists():
                    continue
                if main_video_path is not None and vid_path == main_video_path:
                    continue
                try:
                    os.link(str(primary_funscript_path), str(expected_script))
                    hardlinks.append((str(primary_funscript_path), str(expected_script)))
                    logger.info("Hardlinked sibling funscript: %s -> %s",
                                primary_funscript_path.name, expected_script.name)
                except OSError as e:
                    logger.warning("Failed to hardlink sibling funscript for %s: %s",
                                   vid_path.name, e)

        # ─── Write .linkinfo ───
        if hardlinks:
            linkinfo_path = output_dir / ".linkinfo"
            today = date.today().isoformat()
            lines = []
            for original, linked in hardlinks:
                lines.append("[hardlink]")
                lines.append(f"original={original}")
                lines.append(f"linked={linked}")
                lines.append(f"created={today}")
                lines.append("")
            try:
                linkinfo_path.write_text("\n".join(lines), encoding="utf-8")
                logger.info("Wrote .linkinfo with %d entries", len(hardlinks))
            except OSError as e:
                logger.error("Failed to write .linkinfo: %s", e)

        pair.organized = True

    def _undo_organize(self, pair: Pair) -> None:
        """Reverse _organize_output(): restore original filenames, remove alt subfolders."""
        import shutil

        if not pair.organized or not pair.original_filenames:
            logger.warning("Pair '%s' not organized or missing original filenames", pair.name)
            return

        output_dir = Path(pair.output_dir)

        # Phase 1: Delete .linkinfo and remove hardlinked videos in alt subfolders
        linkinfo = output_dir / ".linkinfo"
        if linkinfo.exists():
            linkinfo.unlink(missing_ok=True)

        # Phase 2: Move scripts from alt subfolders back to root
        for sub in sorted(output_dir.iterdir()):
            if not sub.is_dir() or not sub.name.endswith((".alt",)) and ".alt" not in sub.name:
                continue
            # Check it's an erodeck alt folder (name contains .alt)
            if ".alt" not in sub.name:
                continue
            for f in sub.iterdir():
                if f.suffix.lower() == ".funscript":
                    # Move script back to root (will be renamed to original name below)
                    target = output_dir / f.name
                    if not target.exists():
                        f.rename(target)
                else:
                    # Hardlinked video — just delete
                    f.unlink(missing_ok=True)
            # Remove empty alt dir
            try:
                sub.rmdir()
            except OSError:
                shutil.rmtree(sub, ignore_errors=True)

        # Phase 3: Rename all items back to original filenames
        for item in pair.items:
            orig = pair.original_filenames.get(item.id)
            if not orig or orig == item.filename:
                continue
            current_path = output_dir / item.filename
            orig_path = output_dir / orig
            if current_path.exists() and not orig_path.exists():
                try:
                    current_path.rename(orig_path)
                    item.filename = orig
                    logger.info("Restored: %s -> %s", current_path.name, orig)
                except OSError as e:
                    logger.error("Failed to restore %s: %s", current_path.name, e)

        pair.organized = False
        logger.info("Undo organize complete: %s", pair.name)

    async def _recover_complete_items(self, pair: Pair, output_dir: Path) -> None:
        """Recover items whose data is already fully on disk.

        After a crash, duplicate-launch race, or interrupted merge, segment
        temp files may be 100 % downloaded even though the item never reached
        COMPLETED.  This method detects that situation, merges the segments,
        and marks the item done — avoiding a pointless (and possibly
        impossible) re-download.
        """
        for item in pair.items:
            if item.state == ItemState.COMPLETED:
                continue

            # Case 1: final output file already exists with correct size
            final_file = output_dir / item.filename
            if (
                item.total_bytes > 0
                and final_file.exists()
                and final_file.stat().st_size >= item.total_bytes
            ):
                item.downloaded_bytes = item.total_bytes
                item.state = ItemState.COMPLETED
                item.error_message = ""
                logger.info("Recovered (output exists): %s", item.filename)
                if self.on_item_updated:
                    self.on_item_updated(item)
                continue

            # Case 2: all segment temp files present and sum to expected size
            if not item.segments or item.total_bytes <= 0:
                continue
            try:
                seg_paths = [Path(seg.temp_file) for seg in item.segments]
                if not all(p.exists() for p in seg_paths):
                    continue
                disk_total = sum(p.stat().st_size for p in seg_paths)
            except OSError:
                continue
            if disk_total < item.total_bytes:
                continue

            # Merge segments into output file
            try:
                segments = item.segments  # capture for thread

                def _do_merge():
                    output_dir.mkdir(parents=True, exist_ok=True)
                    with open(final_file, "wb") as out_f:
                        for seg in segments:
                            with open(seg.temp_file, "rb") as in_f:
                                while True:
                                    chunk = in_f.read(CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    out_f.write(chunk)
                    for seg in segments:
                        try:
                            Path(seg.temp_file).unlink(missing_ok=True)
                        except OSError:
                            pass

                await asyncio.to_thread(_do_merge)
                item.downloaded_bytes = item.total_bytes
                item.state = ItemState.COMPLETED
                item.error_message = ""
                item.segments.clear()
                logger.info("Recovered (merged segments): %s", item.filename)
                if self.on_item_updated:
                    self.on_item_updated(item)
            except Exception as e:
                logger.warning(
                    "Recovery merge failed for %s, will re-download: %s",
                    item.filename, e,
                )

        if self.on_pair_updated:
            self.on_pair_updated(pair)

    def _on_item_progress(self, item: PairItem) -> None:
        import time
        now = time.monotonic()
        self._pump_heartbeat = now  # keep watchdog happy during active downloads

        # Throttle all GUI signal emissions to avoid flooding Qt event loop
        last = getattr(self, "_last_pair_progress_time", 0.0)
        if now - last < 0.5:
            return
        self._last_pair_progress_time = now

        if self.on_item_updated:
            self.on_item_updated(item)
        pair = self._find_pair_by_item(item.id)
        if pair and self.on_pair_updated:
            self.on_pair_updated(pair)

    def _on_item_state_change(self, item: PairItem) -> None:
        if self.on_item_updated:
            self.on_item_updated(item)
        # Also refresh the parent pair so its progress/state display stays in sync
        pair = self._find_pair_by_item(item.id)
        if pair and self.on_pair_updated:
            self.on_pair_updated(pair)

    def _find_pair(self, pair_id: str) -> Pair | None:
        for p in self.pairs:
            if p.id == pair_id:
                return p
        return None

    def _find_pair_by_item(self, item_id: str) -> Pair | None:
        for p in self.pairs:
            for i in p.items:
                if i.id == item_id:
                    return p
        return None

    def _get_item_ids(self, pair_id: str) -> set[str]:
        pair = self._find_pair(pair_id)
        if not pair:
            return set()
        return {i.id for i in pair.items}

    @staticmethod
    def _guess_filename(url: str, file_type: str) -> str:
        from urllib.parse import urlparse, unquote

        path = urlparse(url).path
        name = unquote(path.split("/")[-1]) if path else ""

        if not name or name == "/":
            if file_type == "funscript":
                name = "script.funscript"
            else:
                name = "video.mp4"

        return sanitize_filename(name)

    def get_queue_status(self) -> list[dict]:
        return [p.to_dict() for p in self.pairs]
