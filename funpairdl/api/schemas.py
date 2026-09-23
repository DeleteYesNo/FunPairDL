from __future__ import annotations

from pydantic import BaseModel


class PairGroupSpec(BaseModel):
    """One group within a pair: Main or Alt N (a "(Label)" script variant
    next to Main in the flat library layout).

    The same logical Pair gets sent as a list of groups so the backend
    knows which items belong together at organize time. Each Alt group
    can opt out of inheriting Main's other axes at play time.
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
    # Variant label: files become "<work> (<label>)[.axis].funscript"
    # (brackets/path characters dropped, numbered when taken). Empty → the
    # group's scripter, else "Alt". Ignored for Main.
    display_name: str = ""
    # {file url: group label} — how the user arranged a bundle's files in the
    # panel. The backend splits the bundle by these labels instead of by name.
    bundle_plan: dict[str, str] | None = None
    # {video url: [other urls of the same video]} — mirrors / re-encodes the
    # panel decided not to download; tried in order if the chosen one fails.
    alternates: dict[str, list[str]] | None = None


class VideoCandidate(BaseModel):
    url: str
    name: str = ""
    source: str = "OP"            # "OP" | "comment"
    size: int = 0
    height: int = 0
    duration: float | None = None
    priority: float = 99.0
    failed: bool = False          # the panel's probe of it failed


class VideoPlanRequest(BaseModel):
    """Which of a post's video links to download (see core.video_plan)."""
    videos: list[VideoCandidate] = []
    pick_mode: str = ""           # "" = the setting
    min_resolution: str = ""      # "" = the setting's default resolution
    encode_vs_variant: str = ""   # "" = the setting
    decisions: dict[str, str] = {}  # {url: "reencode" | "variant"}
    credits: list[str] = []         # the post's creator names (title "[X]" prefix, OP)


class LookupVideo(BaseModel):
    url: str
    resolved: str = ""
    duration: float | None = None


class LookupScript(BaseModel):
    url: str
    resolved: str = ""
    name: str = ""
    size: int = 0
    duration: float | None = None


class LibraryLookupRequest(BaseModel):
    """Is this post's work already in the library? (see core.library_lookup)"""
    title: str = ""
    videos: list[LookupVideo] = []
    scripts: list[LookupScript] = []


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
    alternates: dict[str, list[str]] | None = None  # {video url: fallback urls}, see PairGroupSpec
    # New grouped interface: each entry becomes its own folder slot
    # (Main = root, Alt N = subfolder), with optional multi-axis inheritance.
    groups: list[PairGroupSpec] | None = None
    preferred_resolution: str = "best"
    auto_rename: bool = True  # Whether to rename files to pair name after download
    eroscripts_cookies: str = ""  # Sent by extension for authenticated downloads
    source_url: str = ""  # the forum topic this was sent from (topic index)
    # An existing work folder to download INTO (the library already holds
    # the video): scripts are reconciled into it as new axes / variants.
    merge_into: str = ""


class TopicRef(BaseModel):
    id: str
    title: str = ""


class TopicStatusRequest(BaseModel):
    """Which of these topics were opened / sent to the queue?"""
    topics: list[TopicRef] = []


class TopicVisitedRequest(BaseModel):
    id: str
    url: str = ""
    title: str = ""


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
