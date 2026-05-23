from __future__ import annotations

from pydantic import BaseModel


class PairGroupSpec(BaseModel):
    """One group within a pair: Main (root) or Alt N (.alt[N-1]/ subfolder).

    The same logical Pair gets sent as a list of groups so the backend
    knows which items belong together at organize time. Each Alt group
    can optionally inherit Main's multi-axis funscripts as hardlinks.
    """
    name: str                                       # "Main" or "Alt 1", "Alt 2", ...
    video_urls: list[str] = []
    script_urls: list[str] = []
    script_authors: dict[str, str] | None = None
    # {url: real_filename} — supplied for bundle files the extension already
    # probed (pixeldrain /u/, mega /file/, ...). Without it the backend can
    # only guess a name from the URL (a random file id), which breaks pair
    # naming and video↔script stem matching when a bundle is sent expanded.
    filenames: dict[str, str] | None = None
    inherit_multi_axis: bool = True                 # ignored for Main
    # Folder/file stem to use inside the Alt's subfolder. The backend
    # appends ".alt" + collision suffix. Empty → fall back to "<topic>.altN".
    # Ignored for Main (root files always use the topic name).
    display_name: str = ""


class AddPairRequest(BaseModel):
    """Request to add a video+script pair to the download queue."""
    name: str
    # Legacy flat-list interface — used when caller doesn't grouping.
    # When `groups` is provided, these are ignored.
    video_urls: list[str] = []
    script_urls: list[str] = []
    script_authors: dict[str, str] | None = None  # {script_url: author_name}
    filenames: dict[str, str] | None = None  # {url: real_filename} for probed files
    # New grouped interface: each entry becomes its own folder slot
    # (Main = root, Alt N = subfolder), with optional multi-axis inheritance.
    groups: list[PairGroupSpec] | None = None
    preferred_resolution: str = "best"
    auto_rename: bool = True  # Whether to rename files to pair name after download
    eroscripts_cookies: str = ""  # Sent by extension for authenticated downloads


class AddLinkRequest(BaseModel):
    """Request to add a single link."""
    url: str
    name: str = ""
    file_type: str = "auto"  # "video", "funscript", or "auto"


class ResolveRequest(BaseModel):
    """Request to resolve a short-url to a direct CDN URL."""
    url: str
    cookies: str = ""


class ProbeRequest(BaseModel):
    """Request to probe a URL for metadata (file size, available formats)."""
    url: str


class PairStatusResponse(BaseModel):
    id: str
    name: str
    state: str
    progress: float
    items: list[dict]


class QueueStatusResponse(BaseModel):
    pairs: list[PairStatusResponse]
    total_pairs: int
    active_pair: str | None = None


class StatusResponse(BaseModel):
    status: str = "ok"
    version: str
    queue_size: int
