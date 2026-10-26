"""Is this post's media already in the library?

Answers the panel's question before anything is sent: a video link the
queue has downloaded before (live queue or archive), or a library folder
whose title matches the post, points at an existing work. The panel then
downloads only what is new — the video is skipped, scripts identical to
the folder's (sha1 of the forum upload, else exact size) are excluded,
new axes and changed scripts merge into the folder as variants.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path

from funpairdl.constants import QUEUE_ARCHIVE_FILE
from funpairdl.core import library as lib

logger = logging.getLogger("funpairdl.library_lookup")

_lock = threading.Lock()
_archive_index: dict[str, dict] = {}       # url -> {"dir", "name", "pair_id"}
_archive_stamp: tuple[int, int] | None = None
_sha1_cache: dict[tuple[str, int, int], str] = {}
_duration_cache: dict[tuple[str, int, int], float | None] = {}

_CDN_SHA1_RE = re.compile(r"/([0-9a-f]{40})\.funscript(?:$|[?#])", re.IGNORECASE)


def _norm_url(u: str) -> str:
    return (u or "").strip().lower().rstrip("/")


def _refresh_archive(path: Path | None = None) -> None:
    """url -> pair for every archived pair, rebuilt when the file changes."""
    global _archive_index, _archive_stamp
    path = path or QUEUE_ARCHIVE_FILE
    try:
        st = path.stat()
        stamp = (st.st_size, int(st.st_mtime))
    except OSError:
        _archive_index, _archive_stamp = {}, None
        return
    if stamp == _archive_stamp:
        return
    index: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(p, dict) or p.get("state") != "completed":
                    continue
                entry = {"dir": p.get("output_dir") or "", "name": p.get("name") or "",
                         "pair_id": p.get("id") or ""}
                for it in p.get("items") or []:
                    for u in (it.get("url"), it.get("resolved_url")):
                        if u:
                            index.setdefault(_norm_url(u), entry)
    except OSError:
        pass
    _archive_index, _archive_stamp = index, stamp


def _live_index(live_pairs: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in live_pairs:
        if getattr(p.state, "value", p.state) != "completed":
            continue
        entry = {"dir": p.output_dir or "", "name": p.name, "pair_id": p.id}
        for it in p.items:
            for u in (it.url, it.resolved_url):
                if u:
                    out.setdefault(_norm_url(u), entry)
    return out


def _file_sha1(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_size, int(st.st_mtime))
    with _lock:
        cached = _sha1_cache.get(key)
    if cached:
        return cached
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    digest = h.hexdigest()
    with _lock:
        _sha1_cache[key] = digest
    return digest


def _work_duration(work_dir: Path, video: Path | None) -> float | None:
    """The work's length: the video's container, else its L0 script."""
    from funpairdl.utils.media_duration import funscript_info, local_media_duration

    def _cached(target: Path, fn) -> float | None:
        try:
            st = target.stat()
        except OSError:
            return None
        key = (str(target), st.st_size, int(st.st_mtime))
        with _lock:
            if key in _duration_cache:
                return _duration_cache[key]
        d = fn(target)
        with _lock:
            _duration_cache[key] = d
        return d

    cloud = _cloud_entries(work_dir)
    if cloud is not None:
        lens = [float(f.get("duration") or 0) for f in cloud
                if video is not None and f["rel"] == video.name]
        return (lens[0] or None) if lens else None
    if video is not None:
        d = _cached(video, local_media_duration)
        if d:
            return d
    # No parseable container: the L0 script's last action is the length too.
    scripts = sorted(f for f in work_dir.iterdir()
                     if f.is_file() and f.name.lower().endswith(".funscript"))
    if not scripts:
        return None
    plain = [f for f in scripts if "." not in f.stem] or scripts
    return _cached(plain[0], lambda f: funscript_info(f.read_bytes()).get("duration"))


def _cloud_entries(d: Path) -> list[dict] | None:
    """A cloud work's top-level files from its root's index ({"rel", "size",
    "duration"?}); None for a local folder. Listing or reading a cloud work
    through the drive is a cloud round trip each."""
    files = lib.cloud_files(d)
    if files is None:
        return None
    return [f for f in files if "/" not in str(f.get("rel") or "")]


def _folder_video(d: Path) -> Path | None:
    """The work's main video: the one named after the folder, else the
    shortest stem (variants carry a " (Label)" suffix)."""
    cloud = _cloud_entries(d)
    try:
        if cloud is not None:
            vids = [d / f["rel"] for f in cloud if Path(f["rel"]).suffix.lower() in lib.VIDEO_EXTS]
        else:
            vids = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in lib.VIDEO_EXTS]
    except OSError:
        return None
    if not vids:
        return None
    named = [f for f in vids if f.stem.lower() == d.name.lower()]
    if named:
        return named[0]
    return min(vids, key=lambda f: (len(f.stem), f.name))


def _has_media(d: Path) -> bool:
    cloud = _cloud_entries(d)
    if cloud is not None:
        return any(Path(f["rel"]).suffix.lower() in lib.VIDEO_EXTS
                   or str(f["rel"]).lower().endswith(".funscript") for f in cloud)
    try:
        return any(f.is_file() and (f.suffix.lower() in lib.VIDEO_EXTS
                                    or f.name.lower().endswith(".funscript"))
                   for f in d.iterdir())
    except OSError:
        return False


def find_work_by_title(title: str, roots: list[Path], title_key, match_key) -> Path | None:
    """A library folder named like the post (qualifier tags dropped)."""
    keys = {k for k in (title_key(title or ""), match_key(title or "")) if len(k) >= 4}
    if not keys:
        return None
    for root in roots:
        for d in lib.iter_work_dirs(root):
            if keys & {title_key(d.name), match_key(d.name)} and _has_media(d):
                return d
    return None


# Roles of the post's own video (its chosen source and that one's mirrors):
# only these say the work is this one — a variant or another render in the
# library is another video.
_WORK_ROLES = ("", "chosen", "alternate")


def lookup(title: str, videos: list[dict], scripts: list[dict], live_pairs: list,
           roots: list[Path], title_key, match_key, parse_axis,
           video_index: dict[int, list[Path]] | None = None, same_length=None) -> dict:
    """videos: [{"url", "duration"?, "size"?, "name"?, "role"?}],
    scripts: [{"url", "resolved"?, "name"?, "size"?}].

    The work is found by, in turn: a video URL the queue downloaded; a
    library video of one of the post's video's exact byte count
    (`video_index`: {size: [paths]}); a folder named like the post; a work
    `same_length(names, duration)` names — named like the post, holding a
    video of its length (another encode). A library folder renamed since
    is found by the second and fourth.

    Returns::

        {"work": {"dir", "base", "video", "duration"} | None,
         "match": "url" | "size" | "title" | "length" | "",
         "same_content": True | False | None,     # the post's video is this work's
         "scripts": {url: "identical" | "changed" | "new" | "unknown"}}
    """
    _refresh_archive()
    live = _live_index(live_pairs)
    work_dir: Path | None = None
    match = ""
    for v in videos:
        for u in (v.get("url"), v.get("resolved")):
            if not u:
                continue
            e = live.get(_norm_url(u)) or _archive_index.get(_norm_url(u))
            if e and e.get("dir"):
                d = Path(e["dir"])
                if d.is_dir() and not lib.in_trash(d) and _has_media(d):
                    work_dir, match = d, "url"
                    break
        if work_dir:
            break
    own = [v for v in videos if (v.get("role") or "") in _WORK_ROLES]
    if work_dir is None and video_index:
        for v in own:
            size = int(v.get("size") or 0)
            held = [p.parent for p in video_index.get(size, [])
                    if size > 1_000_000 and not lib.in_trash(p.parent)]
            if held:
                work_dir, match = held[0], "size"
                break
    if work_dir is None:
        work_dir = find_work_by_title(title, roots, title_key, match_key)
        match = "title" if work_dir else ""
    if work_dir is None and same_length is not None:
        for v in own:
            if v.get("duration"):
                d = same_length([title, v.get("name") or ""], float(v["duration"]))
                if d is not None:
                    work_dir, match = d, "length"
                    break
    if work_dir is None:
        return {"work": None, "match": "", "same_content": None, "scripts": {}}

    video = _folder_video(work_dir)
    duration = _work_duration(work_dir, video)
    base = video.stem if video else work_dir.name

    if match in ("url", "size", "length"):
        same = True
    else:
        # The post's length: its videos' probed durations, else its scripts'
        # (a script's last action is the work's length too — and file-host
        # or yt-dlp probes often report none).
        probed = [float(v.get("duration") or 0) for v in videos if v.get("duration")]
        if not probed:
            probed = [float(s.get("duration") or 0) for s in scripts if s.get("duration")]
        if duration and probed:
            same = any(abs(p - duration) <= max(3.0, 0.02 * max(p, duration)) for p in probed)
        elif not videos:
            same = True          # a script-only post: nothing to disagree with
        else:
            same = None

    # Scripts on disk, by canonical axis: (name, size, local path — None for
    # a cloud work, whose files are judged by size: never read through the drive).
    on_disk: dict[str, list[tuple[str, int, Path | None]]] = {}
    cloud = _cloud_entries(work_dir)
    if cloud is not None:
        listed = [(str(f["rel"]), int(f.get("size") or 0), None) for f in cloud]
    else:
        listed = []
        try:
            for f in work_dir.iterdir():
                if f.is_file():
                    listed.append((f.name, f.stat().st_size, f))
        except OSError:
            pass
    for name, fsize, path in listed:
        if name.lower().endswith(".funscript"):
            ax, _ = parse_axis(name)
            on_disk.setdefault(ax, []).append((name, fsize, path))
    verdicts: dict[str, str] = {}
    for s in scripts:
        url = s.get("url") or ""
        if not url:
            continue
        name = s.get("name") or ""
        size = int(s.get("size") or 0)
        sha1 = ""
        for u in (s.get("resolved"), url):
            m = _CDN_SHA1_RE.search(u or "")
            if m:
                sha1 = m.group(1).lower()
                break
        ax, _ = parse_axis(name) if name else ("L0", "")
        cands = on_disk.get(ax, [])
        if not cands:
            verdicts[url] = "new"
            continue
        identical = False
        for _name, fsize, f in cands:
            if sha1 and f is not None and _file_sha1(f) == sha1:
                identical = True
                break
            if (not sha1 or f is None) and size and fsize == size:
                identical = True
                break
        if identical:
            verdicts[url] = "identical"
        elif sha1 or size:
            verdicts[url] = "changed"
        else:
            verdicts[url] = "unknown"

    disk_scripts = [{"name": name, "size": fsize}
                    for files in on_disk.values() for name, fsize, _ in files]
    return {
        "work": {"dir": str(work_dir), "base": base,
                 "video": video.name if video else "", "duration": duration},
        "match": match,
        "same_content": same,
        "scripts": verdicts,
        # What the folder holds, for the panel to judge a bundle's files by size.
        "disk_scripts": disk_scripts,
    }
