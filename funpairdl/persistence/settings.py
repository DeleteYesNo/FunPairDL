from __future__ import annotations

import json
import logging
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
        return cls()

    def save(self, path: Path = CONFIG_FILE) -> None:
        global _cache, _cache_path, _cache_time
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        # Update cache immediately after save — keyed to this path so a
        # later load() of a *different* path won't return this instance.
        _cache = self
        _cache_path = _cache_key(path)
        _cache_time = time.monotonic()
        logger.debug("Settings saved to %s", path)
