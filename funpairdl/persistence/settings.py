from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from funpairdl.constants import (
    CONFIG_FILE,
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_SEGMENTS,
)

logger = logging.getLogger("funpairdl.persistence.settings")

# In-memory cache to avoid re-reading config.json on every Settings.load() call.
# Hundreds of load() calls per minute were causing unnecessary disk I/O.
# The cache is keyed by path: load()/save() for a different path must not
# return another file's instance (this previously leaked across callers and
# across tests that load from temp paths).
_cache: Settings | None = None
_cache_path: Path | None = None
_cache_time: float = 0
_CACHE_TTL: float = 5.0  # seconds

# Serializes save() across threads (GUI, api-worker cookie sync, dl-thread) —
# two concurrent writers on the same file would interleave and tear it.
_save_lock = threading.Lock()

# Serializes read-modify-write cycles (Settings.update). save()'s _save_lock
# only protects the write itself — two threads doing load→mutate→save would
# still lose one side's change without this.
_update_lock = threading.RLock()


def _cache_key(path: Path) -> Path:
    """Stable key for cache comparisons — absolute, normalized."""
    try:
        return path.resolve()
    except OSError:  # path may not exist yet; fall back to absolute form
        return path.absolute()


@dataclass
class Settings:
    download_dir: str = str(DEFAULT_DOWNLOAD_DIR)
    max_segments: int = DEFAULT_SEGMENTS
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    minimize_to_tray: bool = True
    cookies_from_browser: str = "brave"
    default_resolution: str = "best"  # "best", "2160", "1080", "720", "480", "360"
    script_variant_mode: str = "flat"  # "flat" = all in root, "subfolder" = per-author .alt subfolders
    max_concurrent_pairs: int = 2  # How many pairs (posts) download simultaneously

    # Pixeldrain
    pixeldrain_api_key: str = ""

    # MEGA (login API blocked by MEGA; use session ID from browser instead)
    mega_email: str = ""
    mega_password: str = ""
    mega_sid: str = ""  # Session ID from browser DevTools (F12 → Console → mega.config.sid)

    # GoFile
    gofile_token: str = ""

    # EroScripts account (for auto re-login when session expires)
    eroscripts_username: str = ""
    eroscripts_password: str = ""

    # EroScripts session cookies (auto-saved from embedded browser)
    eroscripts_cookies: str = ""
    # Full cookie backup (CDP JSON) — used to restore QWebEngine cookies on startup
    eroscripts_cookie_jar: list[dict] = field(default_factory=list)

    # Browser session restore
    browser_tabs: list[str] = field(default_factory=list)  # URLs of open tabs
    browser_active_tab: int = 0  # Index of the active tab
    browser_scroll_positions: list[float] = field(default_factory=list)  # Scroll Y per tab

    # Clipboard Watcher
    clipboard_watch_enabled: bool = True
    clipboard_watch_domains: list[str] = field(
        default_factory=lambda: ["pixeldrain.com"]
    )
    clipboard_notify_tray: bool = True
    clipboard_notify_flash: bool = False
    clipboard_dedupe_seconds: int = 30
    clipboard_skip_in_queue: bool = True
    clipboard_dnd_enabled: bool = False

    # Pixeldrain Picker preferences
    pixeldrain_picker_columns: dict = field(
        default_factory=lambda: {
            "name": True,
            "ext": True,
            "size": True,
            "as_type": True,
            "uploaded": False,
            "url": False,
            "id": False,
        }
    )
    pixeldrain_picker_column_widths: dict = field(default_factory=dict)
    pixeldrain_picker_sort_column: int = 1  # 0=checkbox col is hidden from sort
    pixeldrain_picker_sort_order: int = 0   # 0=Asc, 1=Desc
    # "off" | "video_first" (script renamed to video) | "script_first" (video renamed to script)
    default_rename_direction: str = "video_first"
    # Skip the grouping preview dialog when every group is high-confidence
    pixeldrain_skip_preview_when_confident: bool = False

    # Library reconcile: when re-downloading a work that already exists in the
    # library, merge new axes into its folder and route changed scripts to an
    # .alt variant instead of creating a duplicate folder. Scans download_dir
    # plus any extra library_paths.
    reconcile_on_redownload: bool = True
    library_paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> Settings:
        global _cache, _cache_path, _cache_time
        now = time.monotonic()
        key = _cache_key(path)
        if (_cache is not None and _cache_path == key
                and (now - _cache_time) < _CACHE_TTL):
            return _cache

        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.debug("Settings loaded from %s", path)
                instance = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
                _cache = instance
                _cache_path = key
                _cache_time = now
                return instance
            except Exception as e:
                logger.warning("Failed to load settings: %s", e)
                # Preserve the damaged file — config.json carries the browser
                # tabs and cookie jar; returning defaults and letting the next
                # save() overwrite it would silently destroy them.
                try:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    os.replace(path, path.with_name(f"{path.name}.corrupt-{stamp}"))
                    logger.error("Sidelined unreadable settings file as %s.corrupt-%s",
                                 path.name, stamp)
                except OSError:
                    pass
        return cls()

    @classmethod
    def update(cls, mutator, path: Path = CONFIG_FILE) -> Settings:
        """Atomic read-modify-write: locked fresh load → mutator(settings) →
        save. Use this instead of load()+mutate+save() whenever another
        thread might be updating a DIFFERENT field concurrently (cookie sync
        on the worker loop vs. browser-tab snapshot on the GUI thread) —
        unlocked cycles silently revert each other's fields."""
        with _update_lock:
            # Bypass the TTL cache: a stale instance would resurrect old
            # values for every field the mutator doesn't touch.
            global _cache_time
            _cache_time = 0
            settings = cls.load(path)
            mutator(settings)
            settings.save(path)
            return settings

    def save(self, path: Path = CONFIG_FILE) -> None:
        global _cache, _cache_path, _cache_time
        with _save_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: temp file + os.replace, so a kill mid-write can never
            # truncate config.json (it holds the browser tabs + cookie jar).
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(asdict(self), f, indent=2, ensure_ascii=False)
                # Retry: on Windows a concurrent reader (dl-thread pump,
                # API handler) holds a share lock that makes ReplaceFile
                # fail transiently.
                for attempt in range(5):
                    try:
                        os.replace(tmp, path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            # Update cache immediately after save — keyed to this path so a
            # later load() of a *different* path won't return this instance.
            _cache = self
            _cache_path = _cache_key(path)
            _cache_time = time.monotonic()
            logger.debug("Settings saved to %s", path)
