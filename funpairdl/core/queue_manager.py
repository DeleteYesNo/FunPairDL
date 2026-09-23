from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable

import aiohttp

from funpairdl.constants import (
    CHUNK_SIZE,
    COMPLETED_KEEP_LIVE,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_SEGMENTS,
    RESOLVE_TIMEOUT_SECONDS,
)
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
from funpairdl.core import library as lib
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
        # Guards structural changes to self.pairs (append/remove/move/clear),
        # whole-list reassignment of pair.items, and full-queue snapshots.
        # Multiple threads touch the queue (GUI, api-worker, dl-thread,
        # queue-save writer) — RLock so nested calls on one thread are safe.
        # NEVER hold this lock across an await.
        self._pairs_lock = threading.RLock()
        self._current_tasks: list[asyncio.Task] = []      # All active asyncio tasks (across all pairs)
        self._download_tasks: list[DownloadTask] = []      # All active DownloadTask objects (across all pairs)
        self._pair_tasks: dict[str, list[asyncio.Task]] = {}  # pair_id → its asyncio tasks (for pause/cancel)
        self._running = False
        self._pump_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._registry: ProviderRegistry | None = None

        # Pair ids currently being organized/undone — busy-guard so the GUI
        # can't double-fire a rename while one is already running in a thread.
        self._organizing: set[str] = set()

        # Per-item progress throttle timestamps {item_id: monotonic}.
        # Only touched on the dl-thread (DownloadTask progress callbacks).
        self._item_progress_times: dict[str, float] = {}

        # Created in _dl_init (must be born on the dl loop):
        # _probe_sem caps concurrent off-slot metadata probes;
        # _mega_sem caps MEGA downloads to one file GLOBALLY (across pairs) —
        # MEGA throttles per connection, so one file using all segments gets
        # full bandwidth while staying under the connection-reset threshold.
        self._probe_sem: asyncio.Semaphore | None = None
        self._mega_sem: asyncio.Semaphore | None = None

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
        # Receives [pair_dict, ...] for pairs leaving the live queue
        # (app.py wires this to QueueStore.append_archive).
        self.archive_sink: Callable[[list[dict]], None] | None = None

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

        # TLS verification stays ON here. SegmentDownloader relaxes it per-host
        # only when a cert is actually rejected (e.g. a CDN's expired cert), so
        # valid-cert hosts keep full verification.
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        # Recreate Event in the download thread's loop context
        self._pump_wake = asyncio.Event()
        # Loop-bound primitives must be created here, on the dl loop.
        self._probe_sem = asyncio.Semaphore(3)
        self._mega_sem = asyncio.Semaphore(1)
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
        # Close this loop's shared probe session (metadata prober) so exit
        # doesn't spray "Unclosed client session" warnings.
        try:
            from funpairdl.providers.probe import close_probe_session
            await close_probe_session()
        except Exception:
            pass

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
        filenames: dict[str, str] | None = None,
        sizes: dict | None = None,
        bundle_plan: dict[str, str] | None = None,
        source_url: str = "",
        merge_into: str = "",
        alternates: dict[str, list[str]] | None = None,
    ) -> Pair:
        """Add a Pair to the queue.

        `merge_into` = an existing work folder (inside a library root) the
        download lands in — the library already holds the video, so the
        panel sends the scripts alone and organize reconciles them into
        that work. Ignored when the folder is not a library work.

        Either pass `video_urls`/`script_urls` (everything lands in the Main
        group → root folder, legacy behavior), or pass `groups` — a list of
        dicts shaped like the PairGroupSpec schema. When `groups` is set,
        the flat `*_urls` arguments are ignored.

        Group entries:
          {
            "name": "Main" | "Alt 1" | ...,
            "video_urls": [...], "script_urls": [...],
            "script_authors": {url: author},
            "filenames": {url: real_filename},
            "sizes": {url: bytes},        # probed sizes the extension knows
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
                "filenames": filenames or {},
                "sizes": sizes or {},
                "alternates": alternates or {},
                "inherit_multi_axis": False,
            }]

        pair = Pair(name=name, preferred_resolution=preferred_resolution, auto_rename=auto_rename,
                    source_url=(source_url or "").strip())

        folder_name = sanitize_filename(self._clean_title(name))
        # Caller-supplied override (e.g. Pixeldrain picker) lets a single
        # batch land somewhere other than the global default — useful when
        # the default volume is full.
        root = Path(output_dir_override) if output_dir_override else self.download_dir
        target_dir = root / folder_name
        merge_dir = self._merge_target(merge_into)
        if merge_dir is not None:
            target_dir = merge_dir
            logger.info("Pair '%s' merges into existing work: %s", name, merge_dir)

        # A pair for this folder that is still queued/downloading: don't add
        # a second one. A FAILED one is re-queued below, with THIS
        # submission's sources (the user re-sends after picking a working
        # mirror; keeping the old URLs just failed again).
        existing = next(
            (p for p in self.pairs
             if p.output_dir == str(target_dir) and p.state != PairState.COMPLETED),
            None,
        )
        if existing and existing.state != PairState.FAILED:
            logger.info("Pair '%s' already in queue, skipping", name)
            return existing

        pair.output_dir = str(target_dir)
        pair.foreign_files = self._folder_files(target_dir)

        for grp in groups:
            grp_name = grp.get("name", "Main") or "Main"
            grp_videos = grp.get("video_urls") or []
            grp_scripts = grp.get("script_urls") or []
            grp_authors = grp.get("script_authors") or {}
            # Real filenames the extension already knows (probed bundle files);
            # prefer these over guessing a name from the URL's random file id.
            grp_filenames = grp.get("filenames") or {}
            # Probed sizes the extension already fetched — reusing them means
            # Size/ETA show immediately instead of waiting for a resolve slot.
            grp_sizes = grp.get("sizes") or {}
            # Other links to the same video, tried only when the chosen fails.
            grp_alternates = grp.get("alternates") or {}

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
                    provided = grp_filenames.get(url)
                    # Sanitize: a provided name comes from web metadata and is
                    # used directly as the on-disk path, so an unsanitized
                    # "..\\..\\x" would escape the download folder.
                    filename = sanitize_filename(provided) if provided else self._guess_filename(url, "video")
                    item = PairItem(
                        url=url,
                        filename=filename,
                        file_type=FileType.VIDEO,
                        provider_name=provider,
                        total_bytes=int(grp_sizes.get(url) or 0),
                        group=grp_name,
                    )
                    item.alternates = [str(u) for u in (grp_alternates.get(url) or [])
                                       if u and u != url]
                pair.items.append(item)

            for url in grp_scripts:
                provider = detect_provider(url)
                provided = grp_filenames.get(url)
                filename = sanitize_filename(provided) if provided else self._guess_filename(url, "funscript")
                item = PairItem(
                    url=url,
                    filename=filename,
                    file_type=FileType.FUNSCRIPT,
                    provider_name=provider,
                    total_bytes=int(grp_sizes.get(url) or 0),
                    author=grp_authors.get(url, ""),
                    group=grp_name,
                )
                pair.items.append(item)

        # Bundle arrangement from the panel (per group and/or top level).
        for grp in groups:
            for u, lb in (grp.get("bundle_plan") or {}).items():
                if u and (lb or "").strip():
                    pair.bundle_plan[u] = lb.strip()
        for u, lb in (bundle_plan or {}).items():
            if u and (lb or "").strip():
                pair.bundle_plan[u] = lb.strip()

        if existing:
            self._requeue_failed_pair(existing, pair)
            self._record_topic(existing)
            return existing

        with self._pairs_lock:
            self.pairs.append(pair)
        logger.info("Added pair: %s (%d items)", name, len(pair.items))
        self._record_topic(pair)

        if self.on_pair_added:
            self.on_pair_added(pair)
        if self.on_queue_changed:
            self.on_queue_changed()

        # Ensure pump is alive — restart if it died silently
        self._ensure_pump_alive()
        # Wake pump immediately so it picks up the new pair
        self._wake_pump()
        # Fill in missing name/size off-slot so the UI isn't blank while the
        # pair waits for a download slot.
        self.request_metadata_probe(pair)

        return pair

    def _requeue_failed_pair(self, existing: Pair, fresh: Pair) -> None:
        """Re-queue a FAILED pair as the re-submission `fresh` describes it.
        Items whose URL the user kept and that already finished stay
        completed (their files are on disk); every other URL is taken from
        the re-submission as a pending item — so a mirror picked after a
        dead source actually gets downloaded. Prefs, groups and the bundle
        plan follow the re-submission too."""
        old_by_url = {it.url: it for it in existing.items}
        merged: list[PairItem] = []
        changed = 0
        for it in fresh.items:
            old = old_by_url.get(it.url)
            if old is not None and old.state == ItemState.COMPLETED:
                old.group = it.group
                merged.append(old)
            else:
                if old is None:
                    changed += 1
                merged.append(it)
        dropped = [u for u in old_by_url if u not in {i.url for i in fresh.items}]
        existing.items = merged
        # Everything in the folder except what this pair already finished.
        own = {i.filename.lower() for i in merged if i.state == ItemState.COMPLETED and i.filename}
        existing.foreign_files = [n for n in self._folder_files(Path(existing.output_dir))
                                  if n.lower() not in own]
        existing.alt_group_config = dict(fresh.alt_group_config)
        existing.bundle_plan = dict(fresh.bundle_plan)
        existing.source_url = fresh.source_url or existing.source_url
        existing.preferred_resolution = fresh.preferred_resolution
        existing.auto_rename = fresh.auto_rename
        existing.error_message = ""
        existing.state = PairState.QUEUED
        logger.info("Re-queuing failed pair: %s (%d new source(s), %d dropped)",
                    existing.name, changed, len(dropped))
        if self.on_pair_updated:
            self.on_pair_updated(existing)
        # Persist the re-queue — a kill before the next unrelated save would
        # otherwise silently revert it on restart.
        if self.on_save_needed:
            self.on_save_needed()
        self.request_metadata_probe(existing)
        self._ensure_pump_alive()
        self._wake_pump()

    @staticmethod
    def _folder_files(d: Path) -> list[str]:
        try:
            return sorted(f.name for f in d.iterdir() if f.is_file())
        except OSError:
            return []

    def _merge_target(self, merge_into: str) -> Path | None:
        """`merge_into` as a Path when it is an existing work folder directly
        under a library root (never an arbitrary path from the API)."""
        if not (merge_into or "").strip():
            return None
        try:
            d = Path(merge_into).resolve()
        except OSError:
            return None
        if not d.is_dir() or lib.in_trash(d):
            return None
        for root in self._library_dirs():
            try:
                r = root.resolve()
            except OSError:
                continue
            if d.parent == r or (d.parent.name == lib.NO_VIDEO_DIR and d.parent.parent == r):
                return d
        logger.warning("merge_into ignored (not a library work folder): %s", merge_into)
        return None

    @staticmethod
    def _record_topic(pair: Pair) -> None:
        """Note in the topic index that this forum topic produced `pair`."""
        if not pair.source_url:
            return
        m = re.search(r"/t/(?:[^/]+/)?(\d+)", pair.source_url)
        if not m:
            return
        try:
            from funpairdl.persistence.topic_index import get_topic_index
            get_topic_index().record_pair(m.group(1), pair.source_url, pair.name, pair.id, pair.name)
        except Exception as e:  # never let bookkeeping break a send
            logger.debug("topic index update failed: %s", e)

    def _wake_pump(self) -> None:
        """Thread-safe pump wake — can be called from any thread."""
        if self._dl_loop and self._dl_loop.is_running():
            self._dl_loop.call_soon_threadsafe(self._pump_wake.set)
        else:
            self._pump_wake.set()

    def request_metadata_probe(self, pair: Pair) -> None:
        """Schedule an off-slot name/size probe for a pair's blank items.

        Runs on the dl-thread so it never blocks the caller. Purely
        best-effort: if the download loop isn't running yet (tests, early
        startup) the probe is skipped — resolve will fill the fields later.
        """
        loop = self._dl_loop
        if not loop or not loop.is_running():
            return
        pair_id = pair.id

        def _schedule() -> None:
            loop.create_task(self._probe_pair_metadata(pair_id))

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            pass  # loop shut down between the check and the call

    async def _probe_pair_metadata(self, pair_id: str) -> None:
        """Fill in name/size for a pair's PENDING items without a download
        slot — runs on the dl loop. All failures are swallowed (debug log):
        this is a UI nicety, resolve remains the authoritative path."""
        try:
            try:
                from funpairdl.providers.probe import probe_meta
            except ImportError:
                logger.debug("providers.probe unavailable — metadata probe skipped")
                return

            pair = self._find_pair(pair_id)
            if pair is None:
                return
            items = [
                i for i in pair.items
                if not i.is_bundle and i.total_bytes == 0 and i.state == ItemState.PENDING
            ]
            if not items:
                return

            from funpairdl.persistence.settings import Settings
            settings = Settings.load()
            if self._probe_sem is None:  # dl loop started outside _dl_init (tests)
                self._probe_sem = asyncio.Semaphore(3)

            updated = False
            for item in items:
                try:
                    # Bail out if the pair was removed — don't spend minutes
                    # of the shared probe slots on a deleted pair's items.
                    if self._find_pair(pair_id) is None:
                        return
                    async with self._probe_sem:
                        # Re-check: the pump may have started this item while
                        # we waited on the semaphore.
                        if item.state != ItemState.PENDING:
                            continue
                        meta = await probe_meta(
                            item.url, settings=settings, session=self._session
                        )
                    # Re-check again post-await — never clobber an item that
                    # resolve/download already started filling in.
                    if item.state != ItemState.PENDING:
                        continue
                    changed = False
                    if meta.size and meta.size > 0 and item.total_bytes == 0:
                        item.total_bytes = int(meta.size)
                        changed = True
                    if meta.filename:
                        item.filename = sanitize_filename(meta.filename)
                        changed = True
                    if changed:
                        updated = True
                        if self.on_item_updated:
                            self.on_item_updated(item)
                except Exception as e:
                    logger.debug("Metadata probe failed for %s: %s", item.url[:80], e)

            if updated and self.on_save_needed:
                self.on_save_needed()
        except Exception as e:
            logger.debug("Metadata probe pass failed (pair %s): %s", pair_id, e)

    @staticmethod
    def _is_bundle_url(url: str) -> bool:
        """Check if URL is a Pixeldrain list or MEGA folder (may contain multiple files)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")

        # Pixeldrain list (/l/) or filesystem folder (/d/) — both may hold
        # multiple files. /u/ is a single file and is intentionally excluded.
        if "pixeldrain.com" in host and (path.startswith("l/") or path.startswith("d/")):
            return True

        # MEGA folder: /folder/ in URL — but NOT a single file within a
        # folder (mega.nz/folder/H#K/file/FH), which is already one file.
        if (("mega.nz" in host or "mega.co.nz" in host)
                and "/folder/" in url and "/file/" not in url):
            return True

        # MediaFire folder: its files are listed and fetched one by one.
        if (host == "mediafire.com" or host.endswith(".mediafire.com")) and "/folder/" in parsed.path:
            return True

        return False

    def remove_pair(self, pair_id: str) -> None:
        with self._pairs_lock:
            self.pairs = [p for p in self.pairs if p.id != pair_id]
        if self.on_queue_changed:
            self.on_queue_changed()
        if self.on_save_needed:
            self.on_save_needed()

    def move_pair(self, pair_id: str, direction: int) -> None:
        """Move pair up (-1) or down (+1) in queue."""
        with self._pairs_lock:
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

    def snapshot_dicts(self) -> list[dict]:
        """Consistent full-queue snapshot for persistence. Called on the
        queue-save writer thread — the lock keeps it from seeing a pair
        mid-mutation (bundle replacement, auto-split)."""
        with self._pairs_lock:
            return [p.to_dict() for p in self.pairs]

    @staticmethod
    def _archive_dict(pair: Pair) -> dict:
        """Pair dict for the archive — segments stripped (dead weight)."""
        d = pair.to_dict()
        for it in d.get("items", []):
            it["segments"] = []
        return d

    def archive_completed(self, keep: int = COMPLETED_KEEP_LIVE) -> int:
        """Move surplus COMPLETED pairs out of the live queue into the
        archive sink, keeping the `keep` newest (list order — new pairs are
        appended, so the tail is newest). Returns the number archived.

        This is the retention policy that keeps every save/rebuild/status
        pass cheap: without it the queue grows forever (audit [0])."""
        with self._pairs_lock:
            # Pairs with organize/undo in flight are skipped — their files
            # and metadata are being rewritten by a worker thread right now;
            # they get archived on the next completion instead.
            completed = [
                p for p in self.pairs
                if p.state == PairState.COMPLETED and p.id not in self._organizing
            ]
            surplus = len(completed) - max(0, keep)
            if surplus <= 0:
                return 0
            to_archive = completed[:surplus]
            archived_dicts = [self._archive_dict(p) for p in to_archive]
            if self.archive_sink:
                try:
                    self.archive_sink(archived_dicts)
                except Exception as e:
                    # Don't drop pairs we failed to archive — keep them live
                    # and retry on the next completion.
                    logger.error(
                        "Archive sink failed — keeping %d completed pairs live: %s",
                        len(to_archive), e,
                    )
                    return 0
            drop_ids = {p.id for p in to_archive}
            self.pairs = [p for p in self.pairs if p.id not in drop_ids]
        logger.info("Archived %d completed pairs (keep=%d)", len(to_archive), keep)
        if self.on_queue_changed:
            self.on_queue_changed()
        if self.on_save_needed:
            self.on_save_needed()
        return len(to_archive)

    def clear_completed(self) -> int:
        """Remove ALL completed pairs in one batch (one queue-changed, one
        save) — replaces the GUI's old per-pair remove loop that rebuilt the
        tree and saved the queue once per removed pair. Removed pairs go to
        the archive sink first (best-effort). Returns the number removed."""
        with self._pairs_lock:
            completed = [
                p for p in self.pairs
                if p.state == PairState.COMPLETED and p.id not in self._organizing
            ]
            if not completed:
                return 0
            if self.archive_sink:
                try:
                    self.archive_sink([self._archive_dict(p) for p in completed])
                except Exception as e:
                    # User explicitly asked to clear — proceed even if the
                    # archive write failed, but say so.
                    logger.error("Archive sink failed during clear_completed: %s", e)
            drop_ids = {p.id for p in completed}
            self.pairs = [p for p in self.pairs if p.id not in drop_ids]
        logger.info("Cleared %d completed pairs", len(completed))
        if self.on_queue_changed:
            self.on_queue_changed()
        if self.on_save_needed:
            self.on_save_needed()
        return len(completed)

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

        # Resume works by re-queuing, not by waking a paused coroutine:
        # pause_pair() *cancels* the in-flight download tasks (so segment
        # downloads can be retried from disk), which means there is never a
        # live, event-paused coroutine left to resume. The reliable path is
        # to reset the stalled items to PENDING, mark the pair QUEUED, and let
        # the pump restart it. Partial .part files already on disk are picked
        # up by the segment downloader, so a paused item resumes rather than
        # re-downloading from scratch.
        #
        # Restarting both PAUSED and FAILED items here also recovers a pair
        # that an earlier (buggy) resume left stuck in DOWNLOADING with its
        # items still PAUSED and no task running.
        items_to_restart = [
            i for i in pair.items
            if i.state in (ItemState.PAUSED, ItemState.FAILED)
        ]
        if pair.state not in (PairState.PAUSED, PairState.FAILED) and not items_to_restart:
            return

        for item in items_to_restart:
            item.state = ItemState.PENDING
            item.error_message = ""

        pair.state = PairState.QUEUED
        if self.on_pair_updated:
            self.on_pair_updated(pair)
        self._wake_pump()

    def _begin_organize(self, pair_id: str) -> bool:
        """Busy-guard: claim a pair for organize/undo. False if already busy."""
        with self._pairs_lock:
            if pair_id in self._organizing:
                return False
            self._organizing.add(pair_id)
            return True

    def _end_organize(self, pair_id: str) -> None:
        with self._pairs_lock:
            self._organizing.discard(pair_id)

    async def organize_pair_async(self, pair_id: str) -> bool:
        """Trigger file organize (rename) for a completed pair.

        Runs the file work (library reconcile can SHA-256 multi-GB videos)
        in a thread so the calling loop — GUI or api-worker — never blocks.
        Returns False when the pair is missing, not completed, already
        organized, or currently busy."""
        pair = self._find_pair(pair_id)
        if not pair or pair.state != PairState.COMPLETED:
            return False
        if pair.organized:
            return False
        if not self._begin_organize(pair_id):
            return False
        try:
            await asyncio.to_thread(self._organize_output, pair)
        finally:
            self._end_organize(pair_id)
        if self.on_pair_updated:
            self.on_pair_updated(pair)
        if self.on_save_needed:
            self.on_save_needed()
        return True

    async def undo_organize_pair_async(self, pair_id: str) -> bool:
        """Undo file organize for a completed pair, restoring original
        filenames. Threaded like organize_pair_async; False when the pair is
        missing, not completed, not organized, or currently busy."""
        pair = self._find_pair(pair_id)
        if not pair or pair.state != PairState.COMPLETED:
            return False
        if not pair.organized:
            return False
        if not self._begin_organize(pair_id):
            return False
        try:
            await asyncio.to_thread(self._undo_organize, pair)
        finally:
            self._end_organize(pair_id)
        if self.on_pair_updated:
            self.on_pair_updated(pair)
        if self.on_save_needed:
            self.on_save_needed()
        return True

    async def reorganize_pair_async(self, pair_id: str) -> bool:
        """Undo (if organized) then re-run organize, under one busy-guard so
        no other organize can interleave between the two phases."""
        pair = self._find_pair(pair_id)
        if not pair or pair.state != PairState.COMPLETED:
            return False
        if not self._begin_organize(pair_id):
            return False
        try:
            if pair.organized:
                await asyncio.to_thread(self._undo_organize, pair)
            await asyncio.to_thread(self._organize_output, pair)
        finally:
            self._end_organize(pair_id)
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

        # Pick bundle items to expand. Besides those already flagged, also
        # catch items whose URL is a bundle but whose flag is False — e.g.
        # queue entries persisted before this fix, or a single-file MEGA
        # folder that previously failed to expand and got stuck on the raw
        # /folder/ URL. Mark them so the replacement step at the end removes
        # the placeholder instead of leaving the un-downloadable folder URL.
        bundle_items = []
        for i in pair.items:
            if i.state == ItemState.COMPLETED:
                continue
            if i.is_bundle or self._is_bundle_url(i.url):
                i.is_bundle = True
                bundle_items.append(i)
        if not bundle_items:
            return False

        settings = Settings.load()
        new_items = []

        for bundle_item in bundle_items:
            try:
                url = bundle_item.url
                provider = bundle_item.provider_name
                if provider not in ("pixeldrain", "mega", "mediafire"):
                    # A self-healed item may lack a provider name, and one
                    # queued before its host had a provider says "direct".
                    provider = detect_provider(url)
                resolved_files = []

                if provider == "pixeldrain":
                    pd = PixeldrainProvider(api_key=settings.pixeldrain_api_key)
                    # /l/ = legacy list; /d/ (and /api/filesystem/) = folder
                    # tree that may hold per-pack subfolders of video+script.
                    if "/l/" in url:
                        resolved_files = await pd.resolve_list_all(url)
                    else:
                        resolved_files = await pd.resolve_folder_all(url)
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
                elif provider == "mediafire":
                    # Each file stays a file-page URL: its download link
                    # changes per visit, so the provider reads it at resolve.
                    from funpairdl.providers.mediafire import list_folder
                    async with aiohttp.ClientSession() as mf_session:
                        for f in await list_folder(url, mf_session):
                            resolved_files.append(ResolvedFile(
                                direct_url=f["url"],
                                filename=sanitize_filename(f["name"].replace("/", " - ")),
                                total_size=f["size"],
                            ))
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
                        # A MediaFire file page is resolved like any link.
                        resolved_url="" if provider == "mediafire" else rf.direct_url,
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
            with self._pairs_lock:
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

    @staticmethod
    def _dedupe_item_filenames(pair: Pair) -> None:
        """Give every item of a pair a distinct on-disk name. Two attachments
        of one post often resolve to the SAME name — the forum CDN serves the
        author's original upload name, and a "new" and "legacy" take of one
        script are both uploaded as "<Work>.funscript" — and identical names
        in one output folder made the second download overwrite the first
        (the surviving file was then filed as the .alt, the main was lost).
        Later duplicates become "<stem> (2).ext", "(3)", …; organize still
        parses their axis and files the extras as variants.

        The names of files already in the folder that are not the pair's own
        (pair.foreign_files) are taken too: downloading into an existing work
        under the name of its script replaced that script before the library
        reconcile could compare the two."""
        seen: dict[str, PairItem | None] = {n.lower(): None for n in (pair.foreign_files or [])}
        for item in pair.items:
            if item.is_bundle or not item.filename:
                continue
            key = item.filename.lower()
            if key not in seen:
                seen[key] = item
                continue
            if item.state == ItemState.COMPLETED:
                continue  # already on disk under this name — leave it be
            stem, ext = os.path.splitext(item.filename)
            n = 2
            while f"{stem} ({n}){ext}".lower() in seen:
                n += 1
            new_name = f"{stem} ({n}){ext}"
            logger.info("Duplicate filename in pair '%s': %s -> %s",
                        pair.name, item.filename, new_name)
            item.filename = new_name
            seen[new_name.lower()] = item

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
                # Outer timeout must stay ABOVE the providers' internal 120s
                # budgets (yt-dlp/iwara) or their fallback paths dead-code.
                resolved = await asyncio.wait_for(
                    registry.resolve(
                        item.url,
                        cookies_from_browser=settings.cookies_from_browser,
                        preferred_resolution=preferred_resolution,
                    ),
                    timeout=RESOLVE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Resolve timed out after {RESOLVE_TIMEOUT_SECONDS}s: {item.url[:80]}"
                )

            item.resolved_url = resolved.direct_url
            if resolved.headers:
                item.headers = resolved.headers
            if resolved.total_size:
                item.total_bytes = resolved.total_size
            if resolved.filename:
                item.filename = resolved.filename

            # Push the freshly-resolved name/size to the UI immediately —
            # without this the row stays blank until the whole pair's
            # resolve gather finishes (bounded by its slowest sibling).
            if self.on_item_updated:
                self.on_item_updated(item)

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

    @staticmethod
    def _switch_to_alternate(item: PairItem, attempt: int) -> bool:
        """After the plain retries, move a failed item onto its next fallback
        link (a mirror / re-encode of the same video). The failed url is kept
        in `tried_urls`; the item is otherwise reset like any retry."""
        if attempt <= 2 or not item.alternates:
            return False
        nxt = item.alternates.pop(0)
        item.tried_urls.append(item.url)
        logger.info("Fallback: %s failed (%s) -> trying %s", item.url[:70],
                    (item.error_message or "")[:60], nxt[:70])
        item.url = nxt
        item.provider_name = detect_provider(nxt)
        item.headers = {}
        return True

    async def _download_mega(self, item: PairItem, output_dir: Path) -> None:
        """Download a file from MEGA using built-in decryption (no mega.py)."""
        try:
            # Skip if the output file already exists with the expected size
            if item.total_bytes > 0:
                final = output_dir / item.filename
                if self._already_on_disk(item, final):
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
                elif sid_info.get("auth_error"):
                    logger.warning(
                        "MEGA session invalid (%s) — falling back to anonymous. "
                        "Open mega.nz in embedded browser to refresh.",
                        sid_info["error"],
                    )
                    settings.mega_sid = ""  # Don't use expired sid
                else:
                    # Transient server hiccup (overload/throttle/network) —
                    # the sid is probably fine, keep using it rather than
                    # crippling the download with anonymous bandwidth limits.
                    logger.warning(
                        "MEGA sid validation inconclusive (%s) — keeping session "
                        "and proceeding.",
                        sid_info["error"],
                    )
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

    async def _download_hls(self, item: PairItem, output_dir: Path, preferred_resolution: str = "best",
                            manifest_url: str = "") -> None:
        """Download an HLS stream using yt-dlp — from the page URL, or from
        `manifest_url` when the provider found the stream on a page yt-dlp
        has no extractor for."""
        try:
            # Skip if the output file already exists with the expected size
            if item.total_bytes > 0:
                final = output_dir / item.filename
                if self._already_on_disk(item, final):
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

            original_url = manifest_url or item.url  # yt-dlp reads the page, else the stream

            # yt-dlp runs in a worker thread, so its progress hook must hop
            # back to the event loop to touch UI callbacks safely. Capture the
            # loop here (we're on it now) and throttle refreshes to ~2/sec so
            # the GUI doesn't need a manual pause/resume to update.
            import time as _time
            loop = asyncio.get_running_loop()
            _last_emit = [0.0]

            def _notify_update():
                if self.on_item_updated:
                    loop.call_soon_threadsafe(self.on_item_updated, item)

            def _progress_hook(d):
                status = d.get("status")
                if status == "downloading":
                    item.downloaded_bytes = d.get("downloaded_bytes") or item.downloaded_bytes
                    item.total_bytes = (
                        d.get("total_bytes") or d.get("total_bytes_estimate")
                        or item.total_bytes
                    )
                    item.speed_bps = int(d.get("speed") or 0)
                    now = _time.monotonic()
                    if now - _last_emit[0] >= 0.5:
                        _last_emit[0] = now
                        _notify_update()
                elif status == "finished":
                    # Fragments done (remux/fixup may follow) — push one update
                    # so the bar reaches 100% instead of resting at ~99.8%.
                    item.speed_bps = 0
                    if item.total_bytes:
                        item.downloaded_bytes = item.total_bytes
                    _notify_update()

            def _download():
                import yt_dlp
                output_dir.mkdir(parents=True, exist_ok=True)
                base = item.filename.rsplit(".", 1)[0] if "." in item.filename else item.filename
                output_template = str(output_dir / base) + ".%(ext)s"

                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "outtmpl": output_template,
                    # HLS playlists are fetched fragment-by-fragment; without
                    # parallelism a single slow CDN connection (~200 KiB/s seen
                    # on xvideos) makes large formats blow past the timeout.
                    # Download fragments concurrently to keep speed up.
                    "concurrent_fragment_downloads": 16,
                    "progress_hooks": [_progress_hook],
                }
                # Use impersonation to bypass Cloudflare (requires curl_cffi)
                try:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    ydl_opts["impersonate"] = ImpersonateTarget(client="chrome")
                except ImportError:
                    pass
                # A provider-found stream is fetched as its player would.
                if manifest_url and (item.headers or {}).get("Referer"):
                    ydl_opts["http_headers"] = {"Referer": item.headers["Referer"]}

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

            # Generous cap: a guard against a truly hung download, not a limit
            # on legitimate large videos. With concurrent fragments a normal
            # HLS finishes in minutes; large/slow ones still need headroom.
            HLS_TIMEOUT = 3600
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(_download), timeout=HLS_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"HLS download timed out after {HLS_TIMEOUT}s: {item.url[:80]}"
                )

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

        # Split a pair that holds multiple distinct works into one pair each,
        # whether the videos came from a bundle we just expanded OR arrived
        # pre-expanded (e.g. the user expanded a bundle's file list before
        # sending). _auto_split_bundle_pair bails on single-video or
        # mirror-only pairs, so this is safe to attempt unconditionally.
        new_pairs = self._auto_split_bundle_pair(pair)
        if new_pairs:
            with self._pairs_lock:
                self.pairs.extend(new_pairs)
                pair.state = PairState.COMPLETED
                pair.items.clear()
            for np in new_pairs:
                logger.info("Auto-split: created pair '%s' (%d items)", np.name, len(np.items))
                if self.on_pair_added:
                    self.on_pair_added(np)
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
        foreign = {n.lower() for n in (pair.foreign_files or [])}
        for item in pair.items:
            if item.state == ItemState.COMPLETED:
                continue
            if item.total_bytes > 0 and item.filename.lower() not in foreign:
                final = output_dir / item.filename
                if self._already_on_disk(item, final):
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
        self._dedupe_item_filenames(pair)

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
        # One MEGA file at a time — GLOBALLY, not per pair. MEGA throttles
        # per-connection, so total speed is bounded by total connections
        # regardless of how they're split across files; a single file using
        # all ~32 segments gets full bandwidth (~8 MB/s) without crossing the
        # ~48-connection reset threshold that "files × segments" would hit if
        # two pairs each ran a MEGA file in parallel.
        if self._mega_sem is None:  # dl loop started outside _dl_init (tests)
            self._mega_sem = asyncio.Semaphore(1)
        mega_sem = self._mega_sem

        async def _mega_with_limit(item, out_dir):
            async with mega_sem:
                return await self._download_mega(item, out_dir)

        # A video with fallback links gets one extra round per link: when
        # its url has failed for good, the next mirror/re-encode takes over.
        max_rounds = MAX_ITEM_RETRIES + 1 + max(
            (len(i.alternates) for i in pair.items), default=0)
        for attempt in range(max_rounds):
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
                # Past the plain retries, only items with a fallback left go on.
                if attempt > MAX_ITEM_RETRIES:
                    failed_items = [i for i in failed_items if i.alternates]
                    if not failed_items:
                        break

                delay = 5 * (2 ** (min(attempt, MAX_ITEM_RETRIES) - 1))
                logger.info(
                    "Retrying %d failed item(s) in %ds (attempt %d/%d): %s",
                    len(failed_items), delay, attempt + 1, max_rounds,
                    pair.name,
                )
                await asyncio.sleep(delay)

                if pair.state == PairState.PAUSED:
                    break

                for item in failed_items:
                    self._switch_to_alternate(item, attempt)
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
                self._dedupe_item_filenames(pair)
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
                    task = asyncio.create_task(self._download_hls(
                        item, output_dir, pair.preferred_resolution, resolved.manifest_url))
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
                if attempt < max_rounds - 1 and any(
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
        # Also defensively drop their segment records — dead weight that once
        # accounted for ~75% of queue.json (audit [0]); DownloadTask clears
        # them on COMPLETED, this catches every other completion path.
        for item in pair.items:
            if item.state == ItemState.COMPLETED:
                if item.total_bytes > 0:
                    item.downloaded_bytes = item.total_bytes
                if item.segments:
                    item.segments.clear()

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

        # Persist queue when a pair finishes (save is debounced — cheap)
        if pair.state in (PairState.COMPLETED, PairState.FAILED):
            if self.on_save_needed:
                self.on_save_needed()

        # Retention: push surplus completed pairs out to the archive so the
        # live queue never grows unboundedly again.
        if pair.state == PairState.COMPLETED:
            try:
                self.archive_completed()
            except Exception as e:
                logger.error("archive_completed failed after pair finish: %s", e)

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

    @classmethod
    def _video_identity(cls, v: PairItem) -> str:
        """The most reliable name for a video: a descriptive URL slug
        (rule34video's /video/<id>/beta-samplekit/, iwara's
        /video/<id>/work-title) — stable, and what script authors name their
        files after — else the filename (the real resolved name on file
        hosts whose slug is an opaque token)."""
        slug = Path(cls._guess_filename(v.url, "video")).stem
        # Route words (hanime1's /watch, /view, /embed…) and bare ids are
        # not names — one split once produced a folder literally called
        # "watch".
        descriptive = (("-" in slug or " " in slug)
                       and slug.lower() not in cls._NON_NAME_SLUGS)
        return slug if descriptive else Path(v.filename).stem

    _NON_NAME_SLUGS = frozenset({
        "video", "videos", "index", "watch", "view", "embed", "player",
        "post", "posts", "file", "files", "download", "d", "u", "f",
    })

    @classmethod
    def _mirror_key(cls, name: str) -> str:
        """Key on which two videos count as the same work: drops a site
        prefix ("Iwara - ", "Source: "), bracketed tags ("[id] [Source]",
        "(1080p)"), resolution/fps/watermark tokens, then collapses to
        alphanumerics. "Iwara - Work Title [abc123] [Source]" and the slug
        "work-title" both become "worktitle"."""
        import re
        s = name or ""
        if "." in s and not s.endswith("."):
            s = Path(s).stem
        s = re.sub(r"^\s*(?:iwara|source(?:\s*video)?|mirror|video)\s*[-—–:]\s*", "", s, flags=re.IGNORECASE)
        # A leading bracket is an author/release tag that tells works apart
        # ("[Gweda] Mockie" vs "[Teamboobs] Mockie" — see pairing.normalize);
        # brackets after the title are technical tags and go.
        m = re.match(r"^\s*([\[\(【][^\]\)】]*[\]\)】])", s)
        lead = m.group(1) if m else ""
        rest = s[m.end():] if m else s
        rest = re.sub(r"[\[\(【][^\]\)】]*[\]\)】]", " ", rest)
        return cls._match_key(f"{lead} {rest}")

    @staticmethod
    def _clean_title(title: str) -> str:
        """Clean article title: remove common prefixes/tags that aren't part of the name."""
        import re

        cleaned = title.strip()

        # Remove common bracket-wrapped tags at the start or end
        # e.g. (multi-axis), [Multi-Axis], (ZZ-FREE-0002), [Giddora], [cos], etc.
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
        # Second pass: a known axis word with a qualifier glued on
        # (".suckManual", ".twist_v2"). Treating these as the main axis
        # renamed a suction script to "<base>.funscript" and shoved the real
        # stroke script into an .alt folder. The whole component is kept as
        # the display suffix so the scripter's naming survives the rename.
        for part in reversed(parts):
            canonical = cls._axis_from_prefixed(part)
            if canonical:
                return canonical, part
        # No known axis found → main axis (L0)
        return "L0", ""

    @classmethod
    def _axis_from_prefixed(cls, part: str) -> str:
        """Canonical axis for a component like ``suckManual`` / ``roll-v2``:
        a known *word* axis (3+ letters, not the L0/R1 codes) followed by a
        qualifier that starts with an uppercase letter, digit or separator.
        ``rolling`` / ``pitcher`` do not qualify; returns "" when no match."""
        low = part.lower()
        for word, canonical in cls._ERODECK_AXIS_MAP.items():
            if len(word) < 3 or not word.isalpha():
                continue
            if len(low) > len(word) and low.startswith(word):
                rest = part[len(word):]
                if rest[0].isupper() or rest[0].isdigit() or rest[0] in "-_ ":
                    return canonical
        return ""

    def plan_bundle_split(
        self,
        items: list[PairItem],
        plan: dict[str, str] | None = None,
        pair_name: str = "",
        alt_group_config: dict[str, dict] | None = None,
        hints: dict[str, str] | None = None,
        durations: dict[str, float] | None = None,
        links: dict[str, str] | None = None,
    ) -> list[dict] | None:
        """Decide how a multi-work bundle splits into pairs — without touching
        the queue. Returns None when no split is due (one video, or a mirror
        set where every video reduces to the same name), else the groups in
        output order:

            {"name": folder name, "label": user label or "",
             "videos": [PairItem], "scripts": [PairItem], "others": [PairItem]}

        `plan` (item url → group label) is what the user arranged in the
        panel: planned items go to their labelled group and the name-based
        heuristic only places what is left. The /bundle/plan endpoint calls
        this with no plan so the panel can show the heuristic's outcome
        before sending — the preview and the real split are one code path.
        """
        alt_group_config = alt_group_config or {}
        hints = hints or {}
        durations = {k: float(v) for k, v in (durations or {}).items() if v}
        links = {k: v for k, v in (links or {}).items() if v}
        plan = {k: (v or "").strip() for k, v in (plan or {}).items() if (v or "").strip()}
        videos = [i for i in items if i.file_type == FileType.VIDEO]
        scripts = [i for i in items if i.file_type == FileType.FUNSCRIPT]
        others = [i for i in items if i.file_type == FileType.OTHER]

        def _label(it: PairItem) -> str:
            return plan.get(it.url) or plan.get(it.resolved_url or "") or ""

        labels: list[str] = []
        for it in videos + scripts:
            lb = _label(it)
            if lb and lb not in labels:
                labels.append(lb)

        # One video is one work, whatever labels say: labels seeded by a
        # preview of a larger selection (the user then unchecked a video)
        # must not split the lone video from its scripts.
        if len(videos) <= 1:
            return None

        # Don't split a mirror set: when every video reduces to the same
        # work name they're the same work on different hosts (or the same
        # file picked twice) — keep them in one pair. Only split when there
        # are genuinely distinct works (e.g. a 5-pack folder of separate
        # scenes, whether bundle-expanded or sent pre-expanded). A user plan
        # with two or more labels overrides that judgement.
        #
        # "Same work" is judged on the mirror key — a pixeldrain re-upload
        # named "Iwara - Work Title [id] [Source].mp4" and the iwara page
        # itself (slug "work-title") must compare equal, so the site prefix,
        # bracketed tags and resolution noise are dropped first.
        distinct_stems = set(self._work_keys(videos, durations).values())
        distinct_stems.discard("")
        if len(distinct_stems) <= 1 and len(labels) <= 1:
            return None

        # The heuristic only places what the user did not place.
        h_videos = [v for v in videos if not _label(v)]
        h_scripts = [s for s in scripts if not _label(s)]

        def _strip_axis(name: str) -> str:
            base = name
            if base.lower().endswith(".funscript"):
                base = base[: -len(".funscript")]
            parts = base.rsplit(".", 1)
            if len(parts) == 2 and (
                parts[1].lower() in self._ERODECK_AXIS_MAP
                or self._axis_from_prefixed(parts[1])
            ):
                base = parts[0]
            return base

        # Comparison key for matching a script to its video — drops tokens that
        # differ between a video and its script (resolution/fps/watermark) and
        # optionally a leading bracketed prefix. Shared with library reconcile.
        _key = self._match_key

        # Pick the most reliable identity for a video. The display filename is
        # often poisoned for matching: content.js may set it from a nearby
        # header ("Alpha:"), the topic title ("3 small scripts - Alpha | Beta
        # | Zeta" — which contains *every* work's name and falsely matches every
        # script), or "Untitled". A descriptive URL slug (rule34video's
        # /video/<id>/beta-samplekit/) is stable and is exactly what script
        # authors name their files after, so prefer it. Opaque slugs (a
        # pixeldrain /d/<id> file token) aren't descriptive — fall back to the
        # filename, which for those hosts is the real resolved name.
        _identity = self._video_identity

        # (video, identity stem for naming, match key) — longest key first so
        # the most specific video wins a containment match.
        video_info: list[tuple[PairItem, str, str]] = []
        for v in h_videos:
            real = _identity(v)
            video_info.append((v, real, _key(real)))
        video_info.sort(key=lambda x: len(x[2]), reverse=True)

        def _find_video(sk: str):
            if len(sk) < 4:
                return None
            for v, _, vk in video_info:        # exact match wins outright
                if vk and vk == sk:
                    return v
            for v, _, vk in video_info:        # else longest containment
                if vk and (vk in sk or sk in vk):
                    return v
            return None

        # Token-overlap fallback. Containment fails when a video and its script
        # name the same work with reordered/extra tokens — e.g. video
        # "authx-gamma-eng-sub" vs script "Gamma [AuthX]": neither
        # alphanumeric blob contains the other. Matching on shared *tokens*
        # recovers these. Each shared token is weighted by 1/("how many videos
        # carry it"), so a token unique to one video (e.g. "gamma") is a
        # strong signal while a token spread across many videos (author/series
        # words like "authx", "eng") barely counts — which keeps generic
        # tokens from yanking an orphan script onto the wrong video.
        import re as _re

        def _tokens(name: str) -> set[str]:
            s = name.lower()
            s = _re.sub(
                r"(?<![a-z0-9])(?:\d{3,4}p|[248]k|\d{1,3}fps|no[-_ ]?wm|wm)(?![a-z0-9])",
                " ", s,
            )
            # "bds04" is the word "bds" and the number "04": split where
            # letters meet digits so an abbreviated script name can meet the
            # video's number and its acronym.
            s = _re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", s)
            out = {t for t in _re.split(r"[^a-z0-9]+", s) if len(t) >= 3}
            out |= {t for t in _re.split(r"[^a-z0-9]+", s) if t.isdigit() and len(t) == 2}
            return out

        def _acronym(name: str) -> set[str]:
            """{"bds"} for "[Author] Bedroom Diary Series 04": the
            initials of the title words (leading tag dropped), the way
            scripters abbreviate a series."""
            s = _re.sub(r"^\s*[\[\(【][^\]\)】]*[\]\)】]", " ", name)
            words = [w for w in _re.split(r"[^A-Za-z0-9]+", s) if w and w[0].isalpha()]
            if len(words) < 2:
                return set()
            return {"".join(w[0] for w in words).lower()}

        # Descriptive hints (an e621 post's scene tags) join the video's own
        # name tokens: a script called "fox shower" then finds the post
        # tagged "shower_sex" even though no title says so. Hints are
        # weighted by rarity like any token, so tags every post shares
        # ("sex", "oral") decide nothing.
        video_tokens = [
            (v, _tokens(real) | _tokens(hints.get(v.url, "")) | _acronym(real)
             | _acronym(Path(v.filename).stem))
            for v, real, _ in video_info
        ]
        _df: dict[str, int] = {}
        for _, toks in video_tokens:
            for t in toks:
                _df[t] = _df.get(t, 0) + 1

        def _find_video_by_tokens(name: str):
            stoks = _tokens(name)
            best, best_score, second_score = None, 0.0, 0.0
            for v, vtoks in video_tokens:
                shared = stoks & vtoks
                if not shared:
                    continue
                score = sum(1.0 / _df[t] for t in shared)
                if score > best_score:
                    second_score, best_score, best = best_score, score, v
                elif score > second_score:
                    second_score = score
            # Require at least one reasonably distinctive shared token
            # (df<=2 → score>=0.5). A lone generic token won't clear this.
            # A dead heat between two videos (both share only the series
            # name) is no match either — the document-order rescue below
            # places such a script on the video still without one, which
            # beats handing it to whichever video happened to come first.
            if best_score < 0.5 or best_score == second_score:
                return None
            return best

        matched: dict[int, list[PairItem]] = {id(v): [] for v, _, _ in video_info}
        unmatched_scripts: list[PairItem] = []
        # How each script found its video: "plan" | "name" | "link" |
        # "tokens" | "duration" | "order" | "none" — surfaced to the panel
        # so a guessed pairing is visibly a guess.
        basis: dict[int, str] = {}

        # A script's metadata.video_url names its video outright.
        def _find_video_by_link(s: PairItem):
            target = (links.get(s.url) or "").strip().lower().rstrip("/")
            if not target:
                return None
            for v, _, _ in video_info:
                for cand in (v.url, v.resolved_url or ""):
                    if cand and cand.lower().rstrip("/") == target:
                        return v
            return None

        # Duration: the one video whose length is within tolerance of the
        # script's (2 % or 3 s, whichever is larger). Two videos in range →
        # no decision, on purpose.
        def _find_video_by_duration(s: PairItem):
            ds = durations.get(s.url)
            if not ds:
                return None
            hits = []
            for v, _, _ in video_info:
                dv = durations.get(v.url) or durations.get(v.resolved_url or "")
                if not dv:
                    continue
                if abs(dv - ds) <= max(3.0, 0.02 * dv):
                    hits.append(v)
            return hits[0] if len(hits) == 1 else None

        for s in h_scripts:
            base = _strip_axis(s.filename)
            how = ""
            v = _find_video(_key(base))
            if v is None:
                v = _find_video(_key(base, strip_prefix=True))
            if v is not None:
                how = "name"
            if v is None:
                v = _find_video_by_link(s)
                if v is not None:
                    how = "link"
            if v is None:
                v = _find_video_by_tokens(base)
                if v is not None:
                    how = "tokens"
            if v is None:
                v = _find_video_by_duration(s)
                if v is not None:
                    how = "duration"
            if v is not None:
                matched[id(v)].append(s)
                basis[id(s)] = how
            else:
                unmatched_scripts.append(s)

        # Scripts that fit every video equally are the work's scripts, and
        # the videos are its variants: "Work (nude).mp4" + "Work
        # (stockings).mp4" with "Work[.axis].funscript" is one work, not
        # two. Containment gave every script to the video with the longest
        # key and left the other bare; the order rescue below would then deal
        # the axis set out between the two folders. Same-host videos with one
        # mirror key are otherwise kept apart (two booru posts titled alike),
        # so this only applies when their names do differ by a tag and at
        # most one of them holds a name match.
        if not labels and len(videos) >= 2 and len(h_videos) == len(videos):
            mkeys = {self._mirror_key(_identity(v)) for v in videos}
            mkeys.discard("")
            idents = {Path(_identity(v)).stem.lower() for v in videos}
            name_holders = sum(
                1 for v, _, _ in video_info
                if any(basis.get(id(sc)) == "name" for sc in matched[id(v)])
            )
            if len(mkeys) == 1 and len(idents) > 1 and name_holders <= 1:
                return None

        # Document-order rescue for orphan scripts. EroScripts authors usually
        # lay a bundle out as "video, then its script, next video, its
        # script, …", so a script's partner is the video at the same position.
        # Name matching can't recover a pairing the names don't share — e.g. a
        # work whose video is a rule34.xxx post (URL slug just "index.php")
        # while the script is "Delta [AuthX]". Fall back to position: attach
        # each leftover script to the nearest *script-less* video by rank
        # (videos and scripts each keep their document order in pair.items,
        # even though all videos precede all scripts). Name matches are never
        # disturbed — this only places true orphans, and only onto videos that
        # found no script of their own.
        if unmatched_scripts:
            vrank = {id(v): r for r, v in enumerate(h_videos)}
            srank = {id(s): r for r, s in enumerate(h_scripts)}
            still_unmatched: list[PairItem] = []
            for s in unmatched_scripts:
                rs = srank.get(id(s), 0)
                cand = [v for v, _, _ in video_info if not matched[id(v)]]
                if not cand:
                    still_unmatched.append(s)
                    continue
                cand.sort(key=lambda v: (
                    abs(vrank.get(id(v), 0) - rs),
                    0 if vrank.get(id(v), 0) <= rs else 1,
                ))
                matched[id(cand[0])].append(s)
                basis[id(s)] = "order"
            unmatched_scripts = still_unmatched

        # Group naming: prefer a human title over the video's own stem. The
        # stem is `_identity()`'s pick — chosen for script *matching*, so it's
        # often a URL slug (iwara/rule34video "/video/<id>/demo-game-mock-
        # battle-2") that makes a poor folder name. An Alt group carries the
        # real title in `alt_group_config[group].display_name`; the lone OP
        # (Main) video inherits the post/bundle title. Fall back to the stem
        # only when neither is available (a plain multi-file folder, no alts).
        main_video_count = sum(
            1 for vi, _, _ in video_info if (vi.group or "Main") == "Main"
        )
        groups: list[dict] = []
        for video_item, real_stem, _ in video_info:
            grp = video_item.group or "Main"
            display = (alt_group_config.get(grp, {}).get("display_name") or "").strip()
            if display:
                title_src = display
            elif grp == "Main" and main_video_count == 1 and pair_name:
                title_src = pair_name
            else:
                title_src = real_stem
            name = sanitize_filename(self._clean_title(title_src))
            if not name:  # title cleaned away to nothing — fall back to the stem
                name = sanitize_filename(self._clean_title(real_stem))
            g_scripts = list(matched[id(video_item)])
            groups.append({
                "name": name, "label": "",
                "videos": [video_item], "scripts": g_scripts,
                "others": [],
                "script_basis": {s.url: basis.get(id(s), "") for s in g_scripts},
            })

        # User-labelled groups, in the order the labels first appear.
        for lb in labels:
            name = sanitize_filename(self._clean_title(lb)) or sanitize_filename(lb) or lb
            g_scripts = [s for s in scripts if _label(s) == lb]
            groups.append({
                "name": name, "label": lb,
                "videos": [v for v in videos if _label(v) == lb],
                "scripts": g_scripts,
                "others": [],
                "script_basis": {s.url: "plan" for s in g_scripts},
            })

        if not groups:
            return None

        # Scripts nobody claimed (likely shared/generic) and "other" files go
        # with the first group.
        if unmatched_scripts:
            groups[0]["scripts"].extend(unmatched_scripts)
            for s in unmatched_scripts:
                groups[0]["script_basis"][s.url] = "none"
        if others:
            groups[0]["others"].extend(others)

        # Group confidence = its weakest script: a group holding one guessed
        # ("order") script is a guess as a whole.
        rank = {"none": 0, "order": 1, "duration": 2, "tokens": 3, "link": 4, "name": 5, "plan": 6}
        for g in groups:
            kinds = list(g["script_basis"].values())
            g["basis"] = min(kinds, key=lambda k: rank.get(k, 0)) if kinds else ""

        # Two works with the same title (two booru posts named alike) must
        # not share a folder: the later ones get the video's own id/slug, or
        # a counter, appended.
        seen: dict[str, int] = {}
        for g in groups:
            base = g["name"]
            n = seen.get(base, 0) + 1
            seen[base] = n
            if n == 1:
                continue
            tag = ""
            if g["videos"]:
                slug = Path(self._guess_filename(g["videos"][0].url, "video")).stem
                if slug and slug.lower() not in ("video", "index") and 0 < len(slug) <= 24:
                    tag = slug
            g["name"] = sanitize_filename(f"{base} [{tag}]" if tag else f"{base} ({n})")
        return groups

    @classmethod
    def _work_keys(cls, videos: list[PairItem],
                   durations: dict[str, float] | None = None) -> dict[int, str]:
        """id(video) → work key. Videos with the same mirror key are one work
        (mirrors on different hosts) — unless they sit on the SAME host under
        different URLs: two e621 posts titled alike are two works, so those
        keep distinct keys (the key plus the URL).

        Two encodes of one work ("Show.mkv" and "Show-P4-RF35.mkv") share a
        name prefix and a length: when one key starts with the other and the
        durations agree within 1.5 s, they are the same work too."""
        from urllib.parse import urlparse
        durations = durations or {}
        keys = {id(v): cls._mirror_key(cls._video_identity(v)) for v in videos}

        def _dur(v: PairItem) -> float:
            return durations.get(v.url) or durations.get(v.resolved_url or "") or 0.0

        # Fold prefix-related keys with matching durations onto the shorter key.
        for a in videos:
            for b in videos:
                ka, kb = keys[id(a)], keys[id(b)]
                if a is b or not ka or not kb or ka == kb:
                    continue
                if len(ka) < len(kb) and kb.startswith(ka) and len(ka) >= 6:
                    da, db = _dur(a), _dur(b)
                    if da and db and abs(da - db) <= 1.5:
                        keys[id(b)] = ka

        by_key: dict[str, list[PairItem]] = {}
        for v in videos:
            by_key.setdefault(keys[id(v)], []).append(v)
        out: dict[int, str] = {}
        for key, group in by_key.items():
            hosts: dict[str, set[str]] = {}
            for v in group:
                host = (urlparse(v.url).hostname or "").lower().removeprefix("www.")
                hosts.setdefault(host, set()).add(v.url)
            # Only a real host counts; bare/relative URLs (tests, local
            # files) have none and stay mirrors.
            same_host_dupes = any(len(urls) > 1 for host, urls in hosts.items() if host)
            # Same host, different URLs, but every length known and equal →
            # two encodes/uploads of one video (a pixeldrain 4K + 1080p pair),
            # not two works. Distinct works of one title differ in length.
            if same_host_dupes:
                ds = [_dur(v) for v in group]
                if all(ds) and max(ds) - min(ds) <= 1.5:
                    same_host_dupes = False
            for v in group:
                out[id(v)] = f"{key}#{v.url}" if (same_host_dupes and key) else key
        return out

    def _auto_split_bundle_pair(self, pair: Pair) -> list[Pair] | None:
        """If a resolved bundle produced multiple videos, split into separate
        pairs by matching each video to its scripts via filename stem — or by
        the user's panel arrangement when the pair carries a bundle_plan.

        Returns new pairs if split occurred, or None if no split needed.
        """
        groups = self.plan_bundle_split(
            pair.items, pair.bundle_plan, pair.name, pair.alt_group_config)
        if not groups:
            return None

        # Each split pair becomes its own folder, so whatever group label
        # items carried from the bundle source is no longer meaningful; reset
        # to Main so organize treats them flatly.
        new_pairs: list[Pair] = []
        for g in groups:
            new_pair = Pair(name=g["name"], preferred_resolution=pair.preferred_resolution,
                            source_url=pair.source_url)
            new_pair.output_dir = str(self.download_dir / g["name"])
            new_pair.items = list(g["videos"]) + list(g["scripts"]) + list(g["others"])
            for it in new_pair.items:
                it.group = "Main"
            new_pairs.append(new_pair)
            self._record_topic(new_pair)

        videos = [i for i in pair.items if i.file_type == FileType.VIDEO]
        logger.info(
            "Auto-split bundle pair '%s' (%d videos) into %d pairs: %s",
            pair.name, len(videos), len(new_pairs),
            ", ".join(f"'{p.name}' ({len(p.items)})" for p in new_pairs),
        )
        return new_pairs

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

        These auto-generated Alt groups inherit Main's multi-axis
        scripts (`inherit_multi_axis=True`): a second L0 posted next to a
        pitch/roll/... set is another stroke take on the same scene, and
        every take is played with the full axis set. Only an explicit Alt
        group from the picker UI can opt out.
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
            pair.alt_group_config.setdefault(alt_name, {"inherit_multi_axis": True})
            for it in items:
                it.group = alt_name

    @staticmethod
    def _adopt_lone_alt_video(pair: Pair, output_dir: Path) -> None:
        """Main has no video but one Alt group brought one (the OP linked no
        video; a commenter did): that video is the work's, so it moves to
        Main instead of landing as "<work> (Alt).mp4" beside nothing."""
        main_videos = [it for it in pair.items
                       if (it.group or "Main") == "Main" and it.file_type == FileType.VIDEO
                       and (output_dir / it.filename).exists()]
        if main_videos:
            return
        alt_videos = [it for it in pair.items
                      if (it.group or "Main") != "Main" and it.file_type == FileType.VIDEO
                      and (output_dir / it.filename).exists()]
        if len(alt_videos) != 1:
            return
        v = alt_videos[0]
        logger.info("Lone video from %s adopted as Main's: %s", v.group, v.filename)
        v.group = "Main"

    _MERGED_SCRIPT_RE = re.compile(r"\.(?:merged|multi-?axis|multiaxis|combined|all-?axes)\.funscript$",
                                   re.IGNORECASE)

    def _drop_redundant_scripts(self, pair: Pair, output_dir: Path) -> None:
        seen: dict[str, PairItem] = {}
        seen_paths: set[str] = set()
        scripts = [it for it in pair.items if it.file_type == FileType.FUNSCRIPT
                   and (output_dir / it.filename).exists()]
        has_axes = any(self._parse_axis(it.filename)[0] != "L0" for it in scripts)
        for it in scripts:
            path = output_dir / it.filename
            # Two items on ONE file (a mirror skipped as already on disk):
            # the file stays, the second item is redundant bookkeeping.
            if path.name.lower() in seen_paths:
                pair.items.remove(it)
                continue
            seen_paths.add(path.name.lower())
            if has_axes and self._MERGED_SCRIPT_RE.search(it.filename):
                path.unlink(missing_ok=True)
                pair.items.remove(it)
                logger.info("Dropped combined multi-axis file (axes present): %s", it.filename)
                continue
            try:
                digest = self._file_sha256(path) + "|" + self._parse_axis(it.filename)[0]
            except OSError:
                continue
            first = seen.get(digest)
            if first is None:
                seen[digest] = it
                continue
            path.unlink(missing_ok=True)
            pair.items.remove(it)
            logger.info("Dropped duplicate script (identical to %s): %s", first.filename, it.filename)

    def _autopromote_extra_main_videos(self, pair: Pair, output_dir: Path) -> None:
        """A second, different video in Main is a variant of the work
        ("Work (nude).mp4" next to "Work (stockings).mp4" with one script
        set): give it an implicit Alt group named by what sets it apart, so
        it lands as `<base> (<tag>).<ext>` with Main's L0 beside it (the alt
        loop in _organize_output copies that). Byte-identical mirrors are
        left to the Main loop, which drops them."""
        main_videos = [
            it for it in pair.items
            if (it.group or "Main") == "Main" and it.file_type == FileType.VIDEO
            and (output_dir / it.filename).exists()
        ]
        if len(main_videos) <= 1:
            return
        primary = main_videos[0]
        used = set(pair.alt_group_config.keys()) | {it.group for it in pair.items if it.group}
        next_n = 1
        for extra in main_videos[1:]:
            if self._same_file(output_dir / extra.filename, output_dir / primary.filename):
                continue
            while f"Alt {next_n}" in used:
                next_n += 1
            alt_name = f"Alt {next_n}"
            used.add(alt_name)
            tag = self._variant_tag(extra.filename, primary.filename)
            pair.alt_group_config[alt_name] = {"inherit_multi_axis": True, "display_name": tag}
            extra.group = alt_name
            logger.info("Second Main video is a variant: %s -> %s (%s)",
                        extra.filename, alt_name, tag or "Alt")

    _TAG_RE = re.compile(r"[\[\(【]([^\]\)】]+)[\]\)】]")

    @classmethod
    def _variant_tag(cls, name: str, primary_name: str) -> str:
        """What sets a variant video apart from the primary one, as a label:
        its bracketed tags the primary lacks ("Work (stockings)" next to
        "Work (nude)" -> "stockings"), else whatever follows their common
        prefix ("Work 4K" next to "Work" -> "4K"). Empty when nothing does,
        and the group label then falls back to the scripter or "Alt"."""
        stem, pstem = Path(name).stem, Path(primary_name).stem
        theirs = {t.strip().lower() for t in cls._TAG_RE.findall(pstem)}
        tags = [t.strip() for t in cls._TAG_RE.findall(stem)
                if t.strip() and t.strip().lower() not in theirs]
        if tags:
            return lib.sanitize_label(" ".join(tags), fallback="")
        i = 0
        while i < min(len(stem), len(pstem)) and stem[i].lower() == pstem[i].lower():
            i += 1
        return lib.sanitize_label(stem[i:], fallback="")

    _VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".flv"}

    # Trailing bracket groups made only of post qualifiers: "(Requested, HQ
    # Multi-Axis Script)", "[Multi-Axis]", "(Suggested)", "(Soft & Hardcore
    # Scripts)". They describe the post, not the work.
    _QUALIFIER_RE = re.compile(
        r"\s*[\(\[（【]\s*(?:(?:requested|suggested|commissioned|hq|multi[- ]?axis|"
        r"single[- ]?axis|free|paid|scripts?|music|action|based|soft|hard(?:core)?|"
        r"simple|updated?|remake|&|and|\+|,|\s)+)\s*[\)\]）】]\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _title_key(cls, name: str) -> str:
        """`_match_key` of a work title with its trailing qualifier tags
        removed, so "Alpha Beta (Requested, HQ Script)" and "Alpha Beta"
        compare equal."""
        s = (name or "").strip()
        while True:
            t = cls._QUALIFIER_RE.sub("", s)
            if t == s:
                break
            s = t
        return cls._match_key(s)

    @staticmethod
    def _match_key(name: str, strip_prefix: bool = False) -> str:
        """Normalized key for matching a work by name: drop resolution/fps/wm
        tokens (and optionally a leading bracketed prefix), collapse to
        alphanumerics. Shared by bundle auto-split and library reconcile."""
        import re
        s = name.lower()
        if strip_prefix:
            s = re.sub(r"^(\s*[\(\[（][^\)\]）]*[\)\]）]\s*)+", "", s)
        # Resolution / frame rate / watermark / codec / rate-factor tokens
        # describe an encode, not the work ("Show-P4-RF35.mkv" is "Show").
        s = re.sub(
            r"(?<![a-z0-9])(?:\d{3,4}p|[248]k|\d{1,3}fps|no[-_ ]?wm|wm"
            r"|x26[45]|h\.?26[45]|hevc|av1|avc|c?rf\d{1,2}|10bit|8bit|hdr)(?![a-z0-9])",
            " ", s,
        )
        # Keep alphanumerics of ANY script (CJK included) — only drop
        # punctuation/space. Using [a-z0-9] here would erase Chinese/Japanese
        # names entirely and make CJK works unmatchable.
        return "".join(c for c in s if c.isalnum())

    @staticmethod
    def _already_on_disk(item: PairItem, final: Path) -> bool:
        """True when `final` already holds this item. A funscript's size is
        exact (Content-Length of a static upload), so only the very same
        size counts: a same-named file of another size is a different
        script (a Filler variant an earlier run filed under the plain name)
        and must not stand in for this one. Video sizes are often
        estimates, so a video counts once the file is at least that big."""
        if item.total_bytes <= 0 or not final.exists():
            return False
        size = final.stat().st_size
        if item.file_type == FileType.FUNSCRIPT:
            return size == item.total_bytes
        return size >= item.total_bytes

    @staticmethod
    def _file_sha256(path: Path) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def _same_file(cls, a: Path, b: Path) -> bool:
        """True only when both paths hold byte-identical content. Size is
        checked first as a cheap reject; sha256 confirms. Any visual
        difference (different character, recolor, re-encode) changes the
        bitstream, so this never reports a variant render as a duplicate."""
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
            return cls._file_sha256(a) == cls._file_sha256(b)
        except OSError:
            return False

    def _library_dirs(self) -> list[Path]:
        """Folders to scan for an existing copy of a work: download_dir plus
        any user-configured library_paths (deduped, existing only)."""
        from funpairdl.persistence.settings import Settings
        s = Settings.load()
        candidates = [self.download_dir] + [Path(p) for p in (s.library_paths or [])]
        seen, out = set(), []
        for p in candidates:
            try:
                rp = p.resolve()
            except OSError:
                rp = p
            if rp in seen or not p.is_dir():
                continue
            seen.add(rp)
            out.append(p)
        return out

    def _work_stem(self, filename: str) -> str:
        """Funscript filename -> work base (strip .funscript and any axis suffix)."""
        name = filename
        if name.lower().endswith(".funscript"):
            name = name[: -len(".funscript")]
        _, suffix = self._parse_axis(filename)
        if suffix and name.lower().endswith("." + suffix.lower()):
            name = name[: -(len(suffix) + 1)]
        return name

    def _reconcile_with_library(self, pair: Pair) -> bool:
        """Merge a re-downloaded work into its existing library copy instead of
        leaving a duplicate: new axes go into the folder, changed scripts become
        (Label) variants, identical files are dropped. Handles a video+script
        re-download AND a script-only re-download. Returns True when absorbed.
        """
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if not getattr(settings, "reconcile_on_redownload", True):
            return False

        out_dir = Path(pair.output_dir)
        videos = [i for i in pair.items if i.file_type == FileType.VIDEO]
        scripts = [i for i in pair.items if i.file_type == FileType.FUNSCRIPT]
        if not scripts or len(videos) > 1:
            return False  # nothing to reconcile / mirror set -> normal organize

        # This download's own files, by resolved PATH (not name): in Case A the
        # download lands in the existing folder, so we must exclude these when
        # scanning for "pre-existing" files; in Case B (different folder) same
        # names are fine because the paths differ.
        def _rp(p: Path) -> Path:
            try:
                return p.resolve()
            except OSError:
                return p
        incoming_paths = {_rp(out_dir / i.filename) for i in pair.items}
        src_video = (out_dir / videos[0].filename) if videos else None
        if src_video is not None and not src_video.exists():
            src_video = None

        dest = dest_video = dest_base = None

        # Case A: the download landed in a folder that already holds an
        # organized copy (files NOT part of this download) — the common case,
        # since the post-title target folder usually IS the existing work.
        try:
            pre = [f for f in out_dir.iterdir()
                   if f.is_file() and _rp(f) not in incoming_paths]
        except OSError:
            pre = []
        pre_video = next((f for f in pre if f.suffix.lower() in self._VIDEO_EXTS), None)
        pre_scripts = [f for f in pre if f.name.lower().endswith(".funscript")]
        if pre_video or pre_scripts:
            dest = out_dir
            dest_video = pre_video
            dest_base = pre_video.stem if pre_video else self._work_stem(pre_scripts[0].name)

        # Case B: search other library folders by normalized name key — the
        # video's own name, and the post title with its qualifier tags
        # dropped ("(Requested, HQ Script)" comes and goes between posts of
        # the same work). The sha256 guard below still decides "same work".
        if dest is None:
            key_name = (Path(videos[0].filename).stem if videos
                        else self._work_stem(scripts[0].filename))
            wkey = self._match_key(key_name)
            wkeys = {k for k in (wkey, self._title_key(pair.name)) if len(k) >= 2}
            if not wkeys:
                return False
            own = out_dir.resolve()
            # Folders another queued/active pair is still writing are not
            # "the library copy" — a sibling from the same auto-split (two
            # booru posts titled alike, differing only by "[id]") was merged
            # into here before its own video had even landed.
            busy: set[Path] = set()
            with self._pairs_lock:
                for other in self.pairs:
                    if other is pair or not other.output_dir:
                        continue
                    if other.state in (PairState.QUEUED, PairState.DOWNLOADING, PairState.PAUSED):
                        busy.add(_rp(Path(other.output_dir)))
            for root in self._library_dirs():
                try:
                    entries = list(root.iterdir())
                except OSError:
                    continue
                for d in entries:
                    if not d.is_dir() or lib.is_meta_dir(d.name) or d.resolve() == own:
                        continue
                    if not wkeys & {self._match_key(d.name), self._title_key(d.name)}:
                        continue
                    if _rp(d) in busy:
                        continue
                    dest = d
                    dest_video = next((d / f.name for f in d.iterdir()
                                       if f.is_file() and f.suffix.lower() in self._VIDEO_EXTS), None)
                    dest_base = dest_video.stem if dest_video else d.name
                    break
                if dest:
                    break
        if dest is None:
            return False

        # A video+script download only merges into a folder that has a video
        # to compare against; with nothing to compare, "same work" is a
        # guess — and the wrong guess strips a work of its script.
        if src_video is not None and dest_video is None:
            return False

        # Same-media guard: only meaningful when BOTH sides have a video.
        if src_video is not None and dest_video is not None:
            try:
                if (src_video.stat().st_size != dest_video.stat().st_size
                        or self._file_sha256(src_video) != self._file_sha256(dest_video)):
                    return False  # different video -> a different work
            except OSError:
                return False

        # Existing axes in dest (excluding this download's own incoming files).
        existing_axis: dict[str, Path] = {}
        for f in dest.iterdir():
            if (f.is_file() and f.name.lower().endswith(".funscript")
                    and _rp(f) not in incoming_paths):
                ax, _ = self._parse_axis(f.name)
                existing_axis.setdefault(ax, f)

        new_axis, changed = [], []
        for s in scripts:
            sp = out_dir / s.filename
            if not sp.exists():
                continue
            ax, suffix = self._parse_axis(s.filename)
            ex = existing_axis.get(ax)
            if ex is None:
                new_axis.append((sp, suffix))
            else:
                try:
                    identical = self._file_sha256(sp) == self._file_sha256(ex)
                except OSError:
                    identical = False
                if identical:
                    sp.unlink(missing_ok=True)
                else:
                    changed.append((sp, suffix, s))

        def _move(sp: Path, target: Path) -> None:
            import shutil
            if sp.resolve() == target.resolve():
                return
            if target.exists():
                sp.unlink(missing_ok=True)
                return
            try:
                sp.rename(target)
            except OSError:
                shutil.move(str(sp), str(target))

        # 1) new axes -> into the existing folder
        for sp, suffix in new_axis:
            name = dest_base + (f".{suffix}" if suffix else "") + ".funscript"
            _move(sp, dest / name)

        # 2) changed scripts -> (Label) variants next to the existing set;
        #    the video is shared, nothing is linked or copied
        overrides: dict[str, dict] = {}
        if changed:
            used = lib.existing_labels(dest, dest_base)
            main_authors = {(i.author or "").strip() for i in scripts
                            if (i.group or "Main") == "Main" and (i.author or "").strip()}
            by_group: dict[str, list[tuple[Path, str, PairItem]]] = {}
            for sp, suffix, s in changed:
                by_group.setdefault(s.group or "Main", []).append((sp, suffix, s))
            for gname, entries in by_group.items():
                items = [e[2] for e in entries]
                authors = {(i.author or "").strip() for i in items if (i.author or "").strip()}
                if gname == "Main":
                    label = lib.sanitize_label(next(iter(authors))) if len(authors) == 1 else "Alt"
                else:
                    label = self._group_label(pair, gname, items, main_authors)
                label = lib.unique_label(label, used)
                used.add(label)
                if len(authors) == 1:
                    overrides[label] = {"author": next(iter(authors))}
                for sp, suffix, _s in entries:
                    _move(sp, dest / lib.script_name(dest_base, label, suffix))

        # 3) drop a duplicate downloaded video; clean up a separate temp folder
        if (src_video is not None and dest_video is not None
                and src_video.resolve() != dest_video.resolve()):
            src_video.unlink(missing_ok=True)
        if dest.resolve() != out_dir.resolve():
            try:
                if out_dir.is_dir() and not any(out_dir.iterdir()):
                    out_dir.rmdir()
            except OSError:
                pass

        self._write_work_sidecar(pair, dest, dest_base, overrides)
        logger.info(
            "Reconciled '%s' into '%s' (%d new axes, %d variant scripts)",
            pair.name, dest.name, len(new_axis), len(changed),
        )
        return True

    def _group_label(self, pair: Pair, gname: str, items: list[PairItem],
                     main_authors: set[str]) -> str:
        """Variant label for an Alt group: the panel's display name, else
        the group's scripter when it is not Main's, else "Alt"."""
        cfg = pair.alt_group_config.get(gname, {})
        disp = (cfg.get("display_name") or "").strip()
        if disp:
            return lib.sanitize_label(disp)
        authors = {it.author.strip() for it in items if (it.author or "").strip()}
        if len(authors) == 1:
            a = next(iter(authors))
            if a.lower() not in {m.lower() for m in main_authors}:
                return lib.sanitize_label(a)
        return "Alt"

    def _schedule_sidecar_enrich(self, work_dir: Path, source_url: str) -> None:
        """Fill the sidecar's forum fields (tags, category, posted_at, OP)
        in the background on the download loop; nothing to do without an
        EroScripts topic or a running loop."""
        if lib.source_site(source_url) != "eroscripts":
            return
        tid = lib.topic_id_from_url(source_url)
        if not tid or not self._dl_loop or not self._dl_loop.is_running():
            return
        session = self._session

        async def _run() -> None:
            try:
                from funpairdl.utils.discourse import enrich_sidecar
                await enrich_sidecar(work_dir, tid, session)
            except Exception as e:
                logger.info("Sidecar enrich skipped for %s: %s", work_dir.name, e)

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._dl_loop)
        except RuntimeError as e:
            logger.debug("Sidecar enrich not scheduled: %s", e)

    def _write_work_sidecar(self, pair: Pair, work_dir: Path, base: str,
                            overrides: dict[str, dict] | None = None,
                            title: str | None = None) -> None:
        try:
            variants = lib.scan_variants(work_dir, base, overrides)
            lib.update_sidecar(work_dir, lib.sidecar_from_pair(pair, variants, title=title))
        except OSError as e:
            logger.error("Failed to write %s in %s: %s", lib.SIDECAR_NAME, work_dir, e)
            return
        self._schedule_sidecar_enrich(work_dir, pair.source_url)

    def _organize_output(self, pair: Pair) -> None:
        """Rename files to the flat library layout (docs/library-layout.md)
        and write the work's ``funlib.json``.

        Grouping is driven by `item.group`:
          - "Main" / ""  → `<base>.mp4`, `<base>[.axis].funscript`
          - "Alt N"      → `<base> (<Label>)[.axis].funscript` next to Main;
                           Label = the group's display name, else its
                           scripter, else "Alt", made unique in the folder.

        No subfolders and no hardlinks: a variant shares Main's video, and
        the axes it lacks are inherited by FunLib at play time
        (`inherit_multi_axis=False` becomes `inherit_axes: false` in the
        sidecar). An Alt group that brought its OWN, different video keeps
        it next to its scripts as `<base> (<Label>).<ext>` — still one work,
        FunLib switches video and thumbnail with the variant.
        """
        from collections import OrderedDict

        # If this work already lives in the library, merge into it (new axes
        # into the folder, changed scripts as (Label) variants) rather than
        # leaving a duplicate folder. Falls through to normal organize on any
        # failure or when there's no existing copy.
        try:
            if self._reconcile_with_library(pair):
                pair.organized = True
                return
        except Exception as e:
            logger.warning(
                "Reconcile failed for '%s' — falling back to normal organize: %s",
                pair.name, e,
            )

        output_dir = Path(pair.output_dir)
        base_name = sanitize_filename(self._clean_title(pair.name))

        # Save original filenames before renaming (for undo)
        if not pair.original_filenames:
            pair.original_filenames = {item.id: item.filename for item in pair.items}

        # A post whose only video was linked in a comment: it is THE video,
        # not a variant of nothing.
        self._adopt_lone_alt_video(pair, output_dir)
        # A pair that downloaded the same script twice (forum attachment +
        # the same file in a pack) keeps one; a combined multi-axis file is
        # redundant next to the axes it merges.
        self._drop_redundant_scripts(pair, output_dir)

        # Backward compat: auto-promote axis/author collisions inside
        # Main into implicit Alt groups (legacy flat-list submissions
        # relied on this).
        self._autopromote_main_collisions(pair)
        self._autopromote_extra_main_videos(pair, output_dir)

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

        # ─── Main group: rename in root ───
        main_items = group_items.get("Main", [])
        main_videos = [i for i in main_items if i.file_type == FileType.VIDEO]
        main_scripts = [i for i in main_items if i.file_type == FileType.FUNSCRIPT]

        main_video_path: Path | None = None

        # Multiple Main videos = mirrors from different hosts. The first
        # wins the rename; a later one that is byte-identical is redundant
        # and dropped, a different one keeps its original name (logged).
        for item in main_videos:
            old_path = output_dir / item.filename
            if not old_path.exists():
                continue
            if main_video_path is None:
                new_name = f"{base_name}{old_path.suffix}"
                new_path = output_dir / new_name
                if old_path != new_path:
                    if new_path.exists():
                        # Target name is taken by a pre-existing copy (e.g. a
                        # re-download into an already-organized work, where
                        # reconcile bailed because this pair had no script).
                        # If the download is byte-identical it's pure
                        # redundancy — drop it instead of leaving a duplicate.
                        # Only when the content differs (a real variant render
                        # or a name collision) do we keep both.
                        if self._same_file(old_path, new_path):
                            old_path.unlink(missing_ok=True)
                            item.filename = new_name
                            main_video_path = new_path
                            logger.info(
                                "Dropped duplicate re-download (identical to existing %s): %s",
                                new_name, old_path.name,
                            )
                        else:
                            logger.warning(
                                "Target file already exists and differs, keeping both: %s",
                                new_path,
                            )
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
            elif old_path != main_video_path and self._same_file(old_path, main_video_path):
                old_path.unlink(missing_ok=True)
                logger.info("Dropped mirror identical to %s: %s", main_video_path.name, old_path.name)
            else:
                logger.warning("Extra Main video kept under its own name: %s", old_path.name)

        # Rename Main scripts; track per-axis primary. If two Main scripts
        # collide on the same axis, the first wins the rename and we log a
        # warning — moving such collisions into a real Alt group is the
        # user's job via the picker UI.
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
            new_name = lib.script_name(base_name, "", suffix)
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

        # ─── Alt groups → flat (Label) variants ───
        used_labels = lib.existing_labels(output_dir, base_name)
        main_authors = {(i.author or "").strip() for i in main_scripts if (i.author or "").strip()}
        main_names = {i.filename.lower() for i in main_items}
        overrides: dict[str, dict] = {}
        if len(main_authors) == 1:
            overrides["Main"] = {"author": next(iter(main_authors))}

        for alt_name in alt_names:
            # Only files that are this group's OWN: an item whose filename
            # is a Main item's filename is the same file (a mirror bundle
            # carried the same upload and was skipped as already on disk) —
            # moving it would steal Main's script.
            alt_items = [i for i in group_items[alt_name]
                         if i.filename.lower() not in main_names
                         and (output_dir / i.filename).exists()]
            if not alt_items:
                logger.info("Alt group %s has no files of its own — nothing to place", alt_name)
                continue
            alt_videos = [i for i in alt_items if i.file_type == FileType.VIDEO]
            alt_scripts = [i for i in alt_items if i.file_type == FileType.FUNSCRIPT]

            label = lib.unique_label(
                self._group_label(pair, alt_name, alt_items, main_authors), used_labels)
            used_labels.add(label)
            cfg = pair.alt_group_config.setdefault(alt_name, {})
            cfg["label"] = label
            authors = {(i.author or "").strip() for i in alt_scripts if (i.author or "").strip()}
            if len(authors) == 1:
                overrides.setdefault(label, {})["author"] = next(iter(authors))

            # An Alt video identical to Main's is the same file twice: drop
            # it. A different one makes this group its own work (below).
            own_video: tuple[PairItem, Path] | None = None
            for v in alt_videos:
                src = output_dir / v.filename
                if main_video_path is not None and main_video_path.exists() \
                        and src != main_video_path and self._same_file(src, main_video_path):
                    src.unlink(missing_ok=True)
                    logger.info("Dropped Alt video identical to Main's: %s", src.name)
                    continue
                if own_video is None:
                    own_video = (v, src)
                else:
                    logger.warning("Extra Alt video kept under its own name: %s", src.name)

            if own_video is not None:
                v, src = own_video
                dest = output_dir / f"{base_name} ({label}){src.suffix}"
                if dest.exists() and dest != src:
                    logger.warning("Target exists, Alt video keeps its name: %s", dest)
                elif dest != src:
                    try:
                        src.rename(dest)
                        v.filename = dest.name
                        logger.info("Variant (%s) video: %s -> %s", label, src.name, dest.name)
                    except OSError as e:
                        logger.error("Failed to rename Alt video %s: %s", src.name, e)

            for s in alt_scripts:
                _canonical, suffix = self._parse_axis(s.filename)
                src = output_dir / s.filename
                if not src.exists():
                    continue
                new_name = lib.script_name(base_name, label, suffix)
                dest = output_dir / new_name
                if dest == src:
                    continue
                if dest.exists():
                    logger.warning("Target exists, skipping: %s", dest)
                    continue
                try:
                    src.rename(dest)
                    s.filename = new_name
                    logger.info("Variant (%s): %s -> %s", label, src.name, new_name)
                except OSError as e:
                    logger.error("Failed to rename %s: %s", src.name, e)
            # A variant that brought its own video but no stroke script is
            # played with the work's scripts. FunLib knows a variant only by
            # its L0 file, so it gets a copy of Main's; the other axes are
            # inherited at play time like any variant's.
            if own_video is not None and not any(
                    self._parse_axis(s.filename)[0] == "L0" for s in alt_scripts):
                import shutil
                main_l0 = output_dir / lib.script_name(base_name, "", "")
                l0_dest = output_dir / lib.script_name(base_name, label, "")
                if main_l0.exists() and not l0_dest.exists():
                    try:
                        shutil.copyfile(main_l0, l0_dest)
                        logger.info("Variant (%s) has a video but no L0: copied %s -> %s",
                                    label, main_l0.name, l0_dest.name)
                    except OSError as e:
                        logger.error("Failed to copy %s for variant %s: %s", main_l0.name, label, e)
            if not bool(cfg.get("inherit_multi_axis", True)):
                overrides.setdefault(label, {})["inherit_axes"] = False

        # A stale .linkinfo from an older layout would describe links that
        # no longer exist.
        linkinfo = output_dir / ".linkinfo"
        if linkinfo.exists() and not any(
                d.is_dir() and lib._ALT_DIR_RE.match(d.name) for d in output_dir.iterdir()):
            linkinfo.unlink(missing_ok=True)

        self._write_work_sidecar(pair, output_dir, base_name, overrides)
        pair.organized = True

    def _undo_organize(self, pair: Pair) -> None:
        """Reverse _organize_output(): restore original filenames, bring
        sibling-work files back, remove the sidecars it wrote and any
        legacy alt subfolders."""
        import shutil

        if not pair.organized or not pair.original_filenames:
            logger.warning("Pair '%s' not organized or missing original filenames", pair.name)
            return

        output_dir = Path(pair.output_dir)

        def _drop_own_sidecar(d: Path) -> None:
            sc = lib.read_sidecar(d)
            if sc and sc.get("pair_id") == pair.id:
                (d / lib.SIDECAR_NAME).unlink(missing_ok=True)

        _drop_own_sidecar(output_dir)

        # Phase 0: sibling work folders made for Alt groups with their own video
        for gname, cfg in pair.alt_group_config.items():
            sib = cfg.get("organized_dir")
            if not sib:
                continue
            sib_dir = Path(sib)
            for item in pair.items:
                if (item.group or "Main") != gname:
                    continue
                orig = pair.original_filenames.get(item.id)
                cur = sib_dir / item.filename
                if not orig or not cur.exists():
                    continue
                target = output_dir / orig
                if target.exists():
                    logger.warning("Cannot restore %s: %s exists", cur, target)
                    continue
                try:
                    cur.rename(target)
                    item.filename = orig
                except OSError as e:
                    logger.error("Failed to restore %s: %s", cur, e)
            _drop_own_sidecar(sib_dir)
            try:
                if sib_dir.is_dir() and not any(sib_dir.iterdir()):
                    sib_dir.rmdir()
            except OSError:
                pass
            cfg.pop("organized_dir", None)

        # Phase 1: legacy layout — .linkinfo and .alt subfolders
        linkinfo = output_dir / ".linkinfo"
        if linkinfo.exists():
            linkinfo.unlink(missing_ok=True)

        # Phase 2: Move scripts from alt subfolders back to root
        for sub in sorted(output_dir.iterdir()):
            if not sub.is_dir() or not lib._ALT_DIR_RE.match(sub.name):
                continue
            for f in sub.iterdir():
                if f.name == lib.SIDECAR_NAME:
                    f.unlink(missing_ok=True)
                    continue
                is_link = False
                try:
                    is_link = f.stat().st_nlink > 1
                except OSError:
                    pass
                if f.suffix.lower() == ".funscript" or not is_link:
                    # A real file (script, or a video of its own) goes back
                    # to the root (renamed to its original name below).
                    target = output_dir / f.name
                    if not target.exists():
                        f.rename(target)
                else:
                    f.unlink(missing_ok=True)   # hardlinked copy of Main's file
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
        foreign = {n.lower() for n in (pair.foreign_files or [])}
        for item in pair.items:
            if item.state == ItemState.COMPLETED:
                continue

            # Case 1: final output file already exists with correct size (a
            # script's size is exact; a file that was there before the pair
            # is never this item's)
            final_file = output_dir / item.filename
            if item.filename.lower() not in foreign and self._already_on_disk(item, final_file):
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

        # Throttle GUI signal emissions PER ITEM (0.5s each). A single shared
        # timestamp let whichever item ticked first starve every concurrent
        # pair's rows (audit [8c]). Only the dl-thread touches this dict.
        last = self._item_progress_times.get(item.id, 0.0)
        if now - last < 0.5:
            return
        self._item_progress_times[item.id] = now
        if len(self._item_progress_times) > 2000:
            # Prune entries that haven't ticked recently — those items are
            # no longer actively downloading.
            cutoff = now - 10.0
            self._item_progress_times = {
                k: v for k, v in self._item_progress_times.items() if v >= cutoff
            }

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

        parsed = urlparse(url)
        path = parsed.path
        # Use the last NON-EMPTY path segment. Many sites (rule34video etc.)
        # end the URL with a trailing slash, so split("/")[-1] is "" and we'd
        # fall back to the generic "video"/"script" name. That generic name
        # then poisons bundle auto-split — every video becomes "video", so the
        # pairs collide on one folder and scripts can't be matched to a video.
        # The real slug (e.g. "authx-gamma-eng-sub") lives one segment back.
        segments = [seg for seg in path.split("/") if seg]
        name = unquote(segments[-1]) if segments else ""

        # Imageboards (rule34.xxx etc.) route everything through "index.php" and
        # carry the real subject in ?tags= — without this the name is a useless
        # "index.php", which both names the folder badly and gives auto-split
        # nothing to match a script against (e.g. an "Delta" script whose video
        # is a rule34.xxx post).
        if name in ("", "index.php", "index") and parsed.query:
            from urllib.parse import parse_qs
            tags = parse_qs(parsed.query).get("tags", [""])[0].strip()
            if tags:
                name = unquote(tags).replace("+", " ").strip()

        if not name:
            name = "script.funscript" if file_type == "funscript" else "video.mp4"

        return sanitize_filename(name)

    def get_queue_status(self) -> list[dict]:
        """Full queue as dicts for the API. Segments are stripped — API
        consumers never need them and they can dwarf the payload."""
        with self._pairs_lock:
            out = []
            for p in self.pairs:
                d = p.to_dict()
                for it in d.get("items", []):
                    it["segments"] = []
                out.append(d)
            return out
