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
    # {url: size_bytes} — probed sizes from the extension's /probe cache
    # (only entries > 0). Lets queued items show Size/ETA immediately
    # instead of waiting for a download slot to resolve.
    sizes: dict[str, int] | None = None
    inherit_multi_axis: bool = True                 # ignored for Main
    # Folder/file stem to use inside the Alt's subfolder. The backend
    # appends ".alt" + collision suffix. Empty → fall back to "<topic>.altN".
    # Ignored for Main (root files always use the topic name).
    display_name: str = ""
    # {file url: group label} — how the user arranged a bundle's files in the
    # panel. The backend splits the bundle by these labels instead of by name.
    bundle_plan: dict[str, str] | None = None


class BundleFileSpec(BaseModel):
    url: str
    name: str = ""
    # Extra words that describe the file but are not part of its name — an
    # e621 post's scene tags ("shower kneeling reverse_cowgirl_position").
    # Used only to match scripts to videos, never for naming.
    hints: str = ""
    # Seconds, when the probe learned it (video container / script's last
    # action). The pairing falls back to it when names and tags tie.
    duration: float | None = None
    # A script's metadata.video_url — names its video outright.
    link: str = ""


class BundlePlanRequest(BaseModel):
    """Ask how a bundle's files would be split into pairs (preview only)."""
    name: str = ""                          # the pair/topic title, for naming
    videos: list[BundleFileSpec] = []
    scripts: list[BundleFileSpec] = []


class AddPairRequest(BaseModel):
    """Request to add a video+script pair to the download queue."""
    name: str
    # Legacy flat-list interface — used when caller doesn't grouping.
    # When `groups` is provided, these are ignored.
    video_urls: list[str] = []
    script_urls: list[str] = []
    script_authors: dict[str, str] | None = None  # {script_url: author_name}
    filenames: dict[str, str] | None = None  # {url: real_filename} for probed files
    sizes: dict[str, int] | None = None  # {url: size_bytes} for probed files (>0 only)
    bundle_plan: dict[str, str] | None = None  # {url: group label}, see PairGroupSpec
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
