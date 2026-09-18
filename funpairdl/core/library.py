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
SIDECAR_VERSION = 2
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


def iter_work_dirs(root: Path, include_no_video: bool = True):
    """Yield every work folder directly under ``root`` (and under
    ``root/No Video/``), skipping meta folders."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for d in entries:
        if not d.is_dir() or is_meta_dir(d.name):
            continue
        if d.name == NO_VIDEO_DIR:
            if include_no_video:
                try:
                    subs = sorted(d.iterdir())
                except OSError:
                    subs = []
                for s in subs:
                    if s.is_dir() and not is_meta_dir(s.name):
                        yield s
            continue
        yield d


# ── naming ───────────────────────────────────────────────────────────────

_LABEL_STRIP_RE = re.compile(r'[()\[\]{}（）【】<>:"/\\|?*\x00-\x1f]')


def sanitize_label(label: str, fallback: str = "Alt") -> str:
    """A variant label: no brackets, no path characters, single spaces,
    at most 60 characters. Case is kept."""
    s = _LABEL_STRIP_RE.sub(" ", label or "")
    s = " ".join(s.split()).strip(" .")
    if len(s) > 60:
        s = s[:60].rstrip(" .")
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
    such as "(CS-FREE-0118)(Crisisbeat)Title" is skipped when another
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


def write_sidecar(work_dir: Path, data: dict) -> Path:
    """Atomic UTF-8 (no BOM) write of ``funlib.json``."""
    p = Path(work_dir) / SIDECAR_NAME
    data = dict(data)
    data.setdefault("version", SIDECAR_VERSION)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def merge_sidecar(existing: dict | None, incoming: dict, overwrite: bool = False) -> dict:
    """Fill ``existing`` with ``incoming``: scalar fields only where the
    existing value is empty (unless ``overwrite``); ``tags`` unioned;
    ``source`` / ``category`` merged key by key the same way; ``variants`` replaced when given, but
    each variant keeps its old ``inherit_axes`` if the new one has none."""
    out = dict(existing or {})
    for k, v in incoming.items():
        if k == "variants":
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
    data["downloaded_at"] = utc_now_iso()
    data["pair_id"] = pair.id
    if variants is not None:
        data["variants"] = variants
    return data


def update_sidecar(work_dir: Path, incoming: dict, overwrite: bool = False) -> dict:
    """read → merge → write; returns what was written."""
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
