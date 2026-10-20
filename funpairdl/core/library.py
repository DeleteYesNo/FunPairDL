"""Library layout shared with FunLib (docs/library-layout.md).

One work = one folder, one video, N script sets ("variants") laid flat:

    <Work>/<Work>.mp4
    <Work>/<Work>.funscript                 Main L0
    <Work>/<Work>.<axis>.funscript          Main other axes
    <Work>/<Work> (<Label>).funscript       variant L0
    <Work>/<Work> (<Label>).<axis>.funscript
    <Work>/<Work> (<Label>).mp4             only when that variant has its OWN video
    <Work>/funlib.json                      sidecar (this module writes it)

Everything under a library root's ``_trash/`` belongs to FunLib's recycle
bin and is invisible to FunPairDL: de-dup, "already on disk", reconcile and
the library tools all skip it. ``_trash/deleted.jsonl`` is read (never
written) to tell which works are currently in the bin.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("funpairdl.library")

SIDECAR_NAME = "funlib.json"
SIDECAR_VERSION = 3   # v3: other_links[]
TRASH_DIR = "_trash"
DELETED_LOG = "deleted.jsonl"
NO_VIDEO_DIR = "No Video"
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".flv"})

_ALT_DIR_RE = re.compile(r"^(?P<stem>.+)\.alt(?P<n>\d*)$", re.IGNORECASE)


# ── roots / meta folders ─────────────────────────────────────────────────

def is_meta_dir(name: str) -> bool:
    """Folders under a library root that are not works and must be treated
    as absent: FunLib's ``_trash``, de-dup quarantine, download temp."""
    n = (name or "").lower()
    return n == TRASH_DIR or n.startswith("_dup_quarantine") or n.startswith(".")


def in_trash(path: Path) -> bool:
    """True when any component of ``path`` is a ``_trash`` folder."""
    return any(p.lower() == TRASH_DIR for p in Path(path).parts)


def real_path(p: Path | str) -> Path:
    """``p.resolve()``, or ``p`` made absolute when it can't be resolved.

    A work folder moved to a cloud drive is a directory junction into an
    rclone (WinFsp) mount, and Windows can't resolve that one
    (``WinError 1005``) although the folder reads fine — it is still a
    library work, never a reason to give up."""
    try:
        return Path(p).resolve()
    except OSError:
        return Path(os.path.abspath(p))


def library_roots(download_dir: Path | str | None = None, extra: list[str] | None = None) -> list[Path]:
    """download_dir plus the configured library_paths, deduped, existing
    only. Reads settings when nothing is passed."""
    if download_dir is None and extra is None:
        from funpairdl.persistence.settings import Settings
        s = Settings.load()
        download_dir, extra = s.download_dir, s.library_paths
    candidates = ([Path(download_dir)] if download_dir else []) + [Path(p) for p in (extra or [])]
    seen: set[Path] = set()
    out: list[Path] = []
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


def has_media(folder: Path) -> bool:
    """A work folder holds a video or a funscript directly inside it; a
    folder of sub-folders (a pack) or of stray files is not a work."""
    try:
        for f in folder.iterdir():
            if f.is_file() and (f.suffix.lower() in VIDEO_EXTS or f.name.lower().endswith(".funscript")):
                return True
    except OSError:
        pass
    return False


def _subdirs(folder: Path) -> list[Path]:
    """Folders directly in ``folder``, sorted, links included but not
    followed: a work moved to a cloud drive is a directory junction into the
    mount, and following ~2,000 of them cost ~20 s per walk of the library.
    A link whose target is gone is yielded too — reading it fails later,
    which callers already handle."""
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if e.is_dir(follow_symlinks=False) or getattr(st, "st_file_attributes", 0) & 0x10:
                    out.append(Path(e.path))  # 0x10: FILE_ATTRIBUTE_DIRECTORY
    except OSError:
        return []
    return sorted(out)


def is_link(path: Path) -> bool:
    """True for a symlink or a directory junction (any reparse point) —
    a work folder that lives on a cloud drive. Never follows it."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return path.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def iter_work_dirs(root: Path, include_no_video: bool = True):
    """Yield every work folder directly under ``root`` (and under
    ``root/No Video/``), skipping meta folders."""
    for d in _subdirs(root):
        if is_meta_dir(d.name):
            continue
        if d.name == NO_VIDEO_DIR:
            if include_no_video:
                for s in _subdirs(d):
                    if not is_meta_dir(s.name):
                        yield s
            continue
        yield d


# ── cloud tier ───────────────────────────────────────────────────────────
# FunLib's tools/cloud-tier.js moves whole work folders to a cloud drive and
# leaves a junction in their place. It writes <root>/.funlib-cloud.json:
# each cloud work's files (sizes, video lengths) and the command that brings
# one back (docs/library-layout.md). Listing a cloud work is a cloud round
# trip, and FunLib does not scan inside them: read the index instead, and
# pull a work back before writing into it.

CLOUD_INDEX = ".funlib-cloud.json"
_cloud_cache: dict[str, tuple[tuple, dict]] = {}
_pull_locks: dict[str, threading.Lock] = {}
_pull_locks_guard = threading.Lock()


def cloud_index(root: Path | str) -> dict:
    """The root's cloud index ({"works": {name: {"files": [...]}}, "pull":
    [...]}), {} without one; re-read only when the file changes."""
    p = Path(root) / CLOUD_INDEX
    try:
        st = p.stat()
    except OSError:
        return {}
    stamp = (st.st_size, st.st_mtime_ns)
    hit = _cloud_cache.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Unreadable cloud index %s: %s", p, e)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("works"), dict):
        return {}
    _cloud_cache[str(p)] = (stamp, data)
    return data


def cloud_files(work_dir: Path) -> list[dict] | None:
    """The files of a cloud work as its index lists them ({"rel", "size",
    and "duration", "w", "h" for a video}); None for a local work or one the
    index does not know."""
    work_dir = Path(work_dir)
    if not is_link(work_dir):
        return None
    entry = (cloud_index(work_dir.parent).get("works") or {}).get(work_dir.name)
    files = entry.get("files") if isinstance(entry, dict) else None
    return [f for f in files if isinstance(f, dict) and f.get("rel")] if isinstance(files, list) else None


def pull_cloud_work(work_dir: Path, timeout: float = 3600) -> bool:
    """Bring a cloud work back to a local folder with the index's pull
    command; True when the folder was in the cloud. Raises when it stays
    there — a download must not write into the cloud drive (FunLib would
    never see the new files)."""
    import subprocess
    work_dir = Path(work_dir)
    with _pull_locks_guard:
        lock = _pull_locks.setdefault(str(work_dir).lower(), threading.Lock())
    with lock:
        if not is_link(work_dir):
            return False
        cmd = cloud_index(work_dir.parent).get("pull")
        if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)):
            raise RuntimeError(f"cloud work has no pull command in {CLOUD_INDEX}: {work_dir.name}")
        logger.info("Pulling cloud work back before writing into it: %s", work_dir)
        t0 = time.monotonic()
        try:
            res = subprocess.run(cmd + [str(work_dir)], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (res.stdout or "") + (res.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"pulling {work_dir.name} back failed: {e}") from e
        if is_link(work_dir) or not work_dir.is_dir():
            last = next((ln for ln in reversed(out.strip().splitlines()) if ln.strip()), "no output")
            raise RuntimeError(f"pulling {work_dir.name} back failed: {last}")
        logger.info("Cloud work is local again (%.0f s): %s", time.monotonic() - t0, work_dir)
        return True


# ── naming ───────────────────────────────────────────────────────────────

_LABEL_STRIP_RE = re.compile(r'[()\[\]{}（）【】<>:"/\\|?*\x00-\x1f]')


def sanitize_label(label: str, fallback: str = "Alt") -> str:
    """A variant label: no brackets, no path characters, single spaces,
    at most 60 characters, no separator at either end ("-max", "_easy" —
    what follows "<Work>" in "<Work>-max" — read "max", "easy"). Case is
    kept."""
    s = _LABEL_STRIP_RE.sub(" ", label or "")
    s = " ".join(s.split()).strip(" ._-~")
    if len(s) > 60:
        s = s[:60].rstrip(" ._-~")
    return s or fallback


def unique_label(label: str, used) -> str:
    """``label`` unless taken (case-insensitively) in ``used``; then
    "<label> 2", "<label> 3", …"""
    taken = {u.lower() for u in used}
    if label.lower() not in taken:
        return label
    n = 2
    while f"{label} {n}".lower() in taken:
        n += 1
    return f"{label} {n}"


def script_name(base: str, label: str = "", suffix: str = "") -> str:
    """On-disk funscript name for (work base, variant label, axis suffix).
    Empty label = Main."""
    stem = f"{base} ({label})" if label else base
    return f"{stem}.{suffix}.funscript" if suffix else f"{stem}.funscript"


def parse_script_name(filename: str, base: str) -> tuple[str, str] | None:
    """(label, axis_suffix) for a flat-layout funscript of work ``base``
    ("" label = Main); None when the file does not belong to the work."""
    name = filename
    if not name.lower().endswith(".funscript"):
        return None
    name = name[: -len(".funscript")]
    if not name.lower().startswith(base.lower()):
        return None
    rest = name[len(base):]
    label = ""
    m = re.match(r"^ \(([^()]+)\)", rest)
    if m:
        label = m.group(1)
        rest = rest[m.end():]
    if rest == "":
        return label, ""
    if rest.startswith(".") and len(rest) > 1 and "/" not in rest:
        return label, rest[1:]
    return None


def parse_media_name(filename: str, base: str) -> str | None:
    """Label of a video file of work ``base``: "" for ``<base>.<ext>``,
    "Soft" for ``<base> (Soft).<ext>``; None when it is not the work's."""
    p = Path(filename)
    if p.suffix.lower() not in VIDEO_EXTS:
        return None
    stem = p.stem
    if stem.lower() == base.lower():
        return ""
    m = re.match(r"^ \(([^()]+)\)$", stem[len(base):]) if stem.lower().startswith(base.lower()) else None
    return m.group(1) if m else None


def alt_dir_label(dirname: str, base: str) -> str:
    """Label for a legacy ``.alt`` / ``.alt1`` subfolder: "Alt", "Alt 1", …
    or the display stem when the folder was named by the user."""
    m = _ALT_DIR_RE.match(dirname)
    if not m:
        return sanitize_label(dirname)
    stem, n = m.group("stem"), m.group("n")
    if stem.lower() != base.lower():
        return sanitize_label(stem)
    return f"Alt {n}" if n else "Alt"


# ── author from "(Author) Title" ─────────────────────────────────────────

_AUTHOR_STOPLIST = re.compile(
    r"^(multi-?axis|single-?axis|unknown|hmv|pmv|cock ?hero|fap ?hero|vr|sbs|[0-9]+p|[0-9]+fps|"
    r"[0-9]+k|request(ed)?|hq script|soft|hard|filler|loop|remake|remastered|uncensored|censored|"
    r"no ?video|script ?only)$", re.IGNORECASE)


def author_from_name(name: str) -> str:
    """"(Author) Title" / "[Author] Title" → "Author". A leading pack code
    such as "(ZZ-FREE-0002)(Crisisbeat)Title" is skipped when another
    group follows; descriptive prefixes ("(Multi-axis)") are not authors.
    Same rule as FunLib's derivation, so both sides agree."""
    rest = str(name or "")
    for _ in range(3):
        m = re.match(r"^\s*[\(\[]([^\)\]]{1,60})[\)\]]\s*(\S)", rest)
        if not m:
            return ""
        group = m.group(1).strip()
        next_is_group = m.group(2) in "(["
        if next_is_group and re.match(r"^[A-Z0-9][A-Z0-9_-]*$", group) and re.search(r"\d", group):
            rest = rest[m.end() - 1:]
            continue
        if _AUTHOR_STOPLIST.match(group):
            if next_is_group:
                rest = rest[m.end() - 1:]
                continue
            return ""
        return group
    return ""


def topic_id_from_url(url: str) -> int | None:
    m = re.search(r"/t/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", url or "")
    return int(m.group(1)) if m else None


def source_site(url: str) -> str:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return ""
    if "eroscripts.com" in host:
        return "eroscripts"
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0]


# ── variants from disk ───────────────────────────────────────────────────

def scan_variants(work_dir: Path, base: str | None = None,
                  overrides: dict[str, dict] | None = None,
                  include_alt_dirs: bool = True) -> list[dict]:
    """Infer ``variants[]`` from a work folder's files. Main = files named
    ``<base>[.axis].funscript``; ``<base> (<Label>)[.axis]`` = variant;
    a ``<base> (<Label>).<ext>`` video is that variant's own video;
    legacy ``.alt*/`` subfolders = variants with relative paths.
    ``overrides`` = {label: {"inherit_axes": False, "author": ...}} merged in."""
    from funpairdl.core.queue_manager import QueueManager

    work_dir = Path(work_dir)
    base = base or work_dir.name
    by_label: dict[str, dict[str, str]] = {}
    videos: dict[str, str] = {}
    order: list[str] = []
    try:
        entries = sorted(work_dir.iterdir())
    except OSError:
        return []

    def _put(label: str, axis: str, rel: str) -> None:
        if label not in by_label:
            by_label[label] = {}
            order.append(label)
        by_label[label].setdefault(axis, rel)

    for f in entries:
        if f.is_file():
            vlabel = parse_media_name(f.name, base)
            if vlabel is not None:
                videos.setdefault(vlabel, f.name)
                continue
            parsed = parse_script_name(f.name, base)
            if parsed is None:
                continue
            label, suffix = parsed
            axis = suffix or "L0"
            _put(label, axis, f.name)
        elif include_alt_dirs and f.is_dir() and _ALT_DIR_RE.match(f.name):
            label = alt_dir_label(f.name, base)
            stem = f.name
            try:
                subs = sorted(f.iterdir())
            except OSError:
                continue
            for s in subs:
                if not s.is_file() or not s.name.lower().startswith(stem.lower()):
                    continue
                if s.suffix.lower() in VIDEO_EXTS:
                    videos.setdefault(label, f"{f.name}/{s.name}")
                    continue
                if not s.name.lower().endswith(".funscript"):
                    continue
                _, suffix = QueueManager._parse_axis(s.name)
                axis = suffix or "L0"
                _put(label, axis, f"{f.name}/{s.name}")

    out: list[dict] = []
    for label in order:
        files = by_label[label]
        v: dict = {"label": label or "Main", "files": files}
        if not label:
            v["primary"] = True
        if label in videos:
            v["video"] = videos[label]
        ov = (overrides or {}).get(label or "Main") or (overrides or {}).get(label)
        if ov:
            for k, val in ov.items():
                if k not in ("label", "files", "primary"):
                    v[k] = val
        out.append(v)
    # Main first
    out.sort(key=lambda v: 0 if v.get("primary") else 1)
    return out


def existing_labels(work_dir: Path, base: str | None = None, include_alt_dirs: bool = True) -> set[str]:
    return {v["label"] for v in scan_variants(work_dir, base, include_alt_dirs=include_alt_dirs)
            if not v.get("primary")}


# ── sidecar ──────────────────────────────────────────────────────────────

def read_sidecar(work_dir: Path) -> dict | None:
    p = Path(work_dir) / SIDECAR_NAME
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as e:
        logger.warning("Unreadable sidecar %s: %s", p, e)
        return None


_sidecar_locks: dict[str, threading.RLock] = {}
_sidecar_locks_guard = threading.Lock()
_tmp_seq = iter(range(1, 1 << 62))


def _sidecar_lock(work_dir: Path) -> threading.RLock:
    """One lock per work folder: a reconcile and the forum enrich finishing
    together wrote through one temp name, and on the cloud mount the second
    open truncated the first's file — the sidecar ended up empty."""
    key = os.path.normcase(os.path.abspath(work_dir))
    with _sidecar_locks_guard:
        return _sidecar_locks.setdefault(key, threading.RLock())


def write_sidecar(work_dir: Path, data: dict) -> Path:
    """Atomic UTF-8 (no BOM) write of ``funlib.json``."""
    p = Path(work_dir) / SIDECAR_NAME
    data = dict(data)
    data.setdefault("version", SIDECAR_VERSION)
    with _sidecar_lock(work_dir):
        tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{next(_tmp_seq)}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    return p


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


OTHER_LINK_FIELDS = ("url", "label", "note", "host")
OTHER_LINKS_MAX = 50


def clean_other_links(links) -> list[dict]:
    """``other_links`` entries as stored: http(s) url required, the known
    string fields only, no duplicate urls, at most OTHER_LINKS_MAX."""
    out: list[dict] = []
    seen: set[str] = set()
    for o in links or []:
        if not isinstance(o, dict):
            continue
        url = str(o.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE) or url in seen:
            continue
        seen.add(url)
        entry = {"url": url}
        for k in OTHER_LINK_FIELDS[1:]:
            v = str(o.get(k) or "").strip()
            if v:
                entry[k] = v[:300]
        out.append(entry)
        if len(out) >= OTHER_LINKS_MAX:
            break
    return out


def merge_other_links(existing, incoming) -> list[dict]:
    """Union by url: existing entries keep their place, a field they lack
    is filled from the incoming one, new urls are appended."""
    out = clean_other_links(existing)
    by_url = {o["url"]: o for o in out}
    for o in clean_other_links(incoming):
        have = by_url.get(o["url"])
        if have is None:
            out.append(o)
            by_url[o["url"]] = o
        else:
            for k, v in o.items():
                have.setdefault(k, v)
    return out[:OTHER_LINKS_MAX]


def merge_sidecar(existing: dict | None, incoming: dict, overwrite: bool = False) -> dict:
    """Fill ``existing`` with ``incoming``: scalar fields only where the
    existing value is empty (unless ``overwrite``); ``tags`` unioned;
    ``source`` / ``category`` merged key by key the same way; ``variants`` replaced when given, but
    each variant keeps its old ``inherit_axes`` if the new one has none."""
    out = dict(existing or {})
    for k, v in incoming.items():
        if k == "variants":
            continue
        if k == "other_links" and isinstance(v, list):
            merged_links = merge_other_links(out.get("other_links"), v)
            if merged_links:
                out["other_links"] = merged_links
            continue
        if k == "tags" and isinstance(v, list):
            # tags are a set: union, keeping order and the existing spelling
            have = [str(t) for t in (out.get("tags") or []) if str(t)]
            seen = {t.lower() for t in have}
            for t in v:
                t = str(t)
                if t and t.lower() not in seen:
                    have.append(t)
                    seen.add(t.lower())
            if have:
                out["tags"] = have
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            sub = dict(out[k])
            for sk, sv in v.items():
                if overwrite or _empty(sub.get(sk)):
                    if not _empty(sv):
                        sub[sk] = sv
            out[k] = sub
        elif overwrite or _empty(out.get(k)):
            if not _empty(v):
                out[k] = v
    if "variants" in incoming and incoming["variants"] is not None:
        old_by_label = {str(v.get("label", "")).lower(): v
                        for v in (out.get("variants") or []) if isinstance(v, dict)}
        merged = []
        for v in incoming["variants"]:
            v = dict(v)
            old = old_by_label.get(str(v.get("label", "")).lower())
            if old and "inherit_axes" not in v and "inherit_axes" in old:
                v["inherit_axes"] = old["inherit_axes"]
            merged.append(v)
        out["variants"] = merged
    out["version"] = SIDECAR_VERSION
    return out


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sidecar_from_pair(pair, variants: list[dict] | None = None, title: str | None = None) -> dict:
    """Offline sidecar fields a Pair knows: title, author, source, pair_id,
    downloaded_at, variants."""
    from funpairdl.core.pair import FileType

    title = title or pair.name
    author = author_from_name(title)
    if not author:
        for it in pair.items:
            if it.file_type == FileType.FUNSCRIPT and (it.group or "Main") == "Main" and it.author:
                author = it.author
                break
    data: dict = {"version": SIDECAR_VERSION, "title": title}
    if author:
        data["author"] = author
    url = (pair.source_url or "").strip()
    if url:
        src: dict = {"site": source_site(url), "url": url}
        tid = topic_id_from_url(url) if src["site"] == "eroscripts" else None
        if tid:
            src["topic_id"] = tid
        data["source"] = src
    links = clean_other_links(getattr(pair, "other_links", None))
    if links:
        data["other_links"] = links
    data["downloaded_at"] = utc_now_iso()
    data["pair_id"] = pair.id
    if variants is not None:
        data["variants"] = variants
    return data


def update_sidecar(work_dir: Path, incoming: dict, overwrite: bool = False) -> dict:
    """read → merge → write as one step per folder; returns what was written."""
    with _sidecar_lock(work_dir):
        merged = merge_sidecar(read_sidecar(work_dir), incoming, overwrite=overwrite)
        write_sidecar(work_dir, merged)
    return merged


# ── FunLib's recycle bin log ─────────────────────────────────────────────

class DeletedLog:
    """Reader of ``<root>/_trash/deleted.jsonl`` across library roots.

    Each ``trash`` event is keyed by its ``id``; later ``restore`` / ``purge``
    events point back with ``ref`` and the last one wins. ``trashed()`` is
    the set of trash events still in the bin. Re-read when a file changes."""

    def __init__(self, roots_fn=None):
        self._roots_fn = roots_fn or library_roots
        self._lock = threading.Lock()
        self._sigs: dict[Path, tuple] = {}
        self._events: dict[Path, list[dict]] = {}
        self._checked = 0.0
        self._ttl = 5.0

    def _log_paths(self) -> list[Path]:
        out = []
        try:
            roots = self._roots_fn()
        except Exception as e:
            logger.debug("library roots unavailable: %s", e)
            return out
        for r in roots:
            out.append(Path(r) / TRASH_DIR / DELETED_LOG)
        return out

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < self._ttl:
            return
        self._checked = now
        for p in self._log_paths():
            try:
                st = p.stat()
                sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                if p in self._events:
                    self._events.pop(p, None)
                    self._sigs.pop(p, None)
                continue
            if self._sigs.get(p) == sig:
                continue
            events: list[dict] = []
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(d, dict):
                            events.append(d)
            except OSError as e:
                logger.warning("deleted.jsonl unreadable (%s): %s", p, e)
                continue
            self._events[p] = events
            self._sigs[p] = sig

    def trashed(self) -> list[dict]:
        """Trash events whose last state is still "in the bin"."""
        with self._lock:
            self._refresh()
            out: list[dict] = []
            for p, events in self._events.items():
                state: dict[str, dict | None] = {}
                for e in events:
                    action = e.get("action")
                    if action == "trash" and e.get("id"):
                        state[e["id"]] = dict(e, _root=str(p.parent.parent))
                    elif action in ("restore", "purge") and e.get("ref") in state:
                        state[e["ref"]] = None
                out.extend(v for v in state.values() if v)
            return out

    @staticmethod
    def folder_of(event: dict) -> str:
        """Work folder name a trash event refers to (from paths, else work)."""
        for pth in event.get("paths") or []:
            parts = [x for x in re.split(r"[\\/]+", str(pth)) if x]
            if not parts:
                continue
            if len(parts) > 1 and parts[0] == NO_VIDEO_DIR:
                return parts[1]
            return parts[0]
        return str(event.get("work") or "")

    def index(self, title_key=None) -> dict:
        """Lookup sets over the current bin: pair ids, topic ids, folder
        names (lower) and their title keys, split by scope (work / variant)."""
        idx = {
            "work": {"pair_ids": set(), "topic_ids": set(), "folders": set(), "folder_keys": set()},
            "variant": {"pair_ids": set(), "topic_ids": set(), "folders": set(), "folder_keys": set()},
            "events": [],
        }
        for e in self.trashed():
            scope = "variant" if str(e.get("scope") or "").lower() == "variant" else "work"
            b = idx[scope]
            if e.get("pair_id"):
                b["pair_ids"].add(str(e["pair_id"]))
            tid = topic_id_from_url(str(e.get("source_url") or ""))
            if tid:
                b["topic_ids"].add(str(tid))
            folder = self.folder_of(e)
            if folder:
                b["folders"].add(folder.lower())
                if title_key:
                    k = title_key(folder)
                    if len(k) >= 4:
                        b["folder_keys"].add(k)
            idx["events"].append(e)
        return idx


_deleted_log: DeletedLog | None = None


def get_deleted_log() -> DeletedLog:
    global _deleted_log
    if _deleted_log is None:
        _deleted_log = DeletedLog()
    return _deleted_log
