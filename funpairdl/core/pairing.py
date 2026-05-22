"""Pair-detection heuristics for matching videos with funscripts.

Used by the Pixeldrain picker (and any future picker) to group selected
files into Pairs before submission. The grouping decision is shown to
the user as a preview — auto-rename is only safe for high/medium
confidence groups.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Strip these segments from filenames before comparing
RESOLUTION_TOKENS = re.compile(
    r"\b(?:2160p?|1440p?|1080p?|720p?|480p?|360p?|4k|uhd|hd|sd)\b",
    re.IGNORECASE,
)
QUALITY_TOKENS = re.compile(
    r"\b(?:remux|bluray|webrip|web-dl|webdl|hdtv|x264|x265|hevc|aac|ac3)\b",
    re.IGNORECASE,
)
# Author / release-group brackets at the start: "[Author]" or "(Author)"
LEADING_BRACKETS = re.compile(r"^\s*[\[(\{][^\]\)\}]+[\]\)\}]\s*")
# Any remaining bracketed content (4K), (v2), [Sub], etc.
ANY_BRACKETS = re.compile(r"[\[(\{][^\]\)\}]*[\]\)\}]")
# Trailing version markers like "_v2", "-final", " (1)"
VERSION_SUFFIX = re.compile(r"[_\-\s]+(?:v\d+|final|fixed|edit\d*|copy)$", re.IGNORECASE)
# Funscript axis suffix like ".roll" / ".pitch" / ".yaw" / ".twist" / ".surge" / ".sway"
AXIS_SUFFIX = re.compile(
    r"\.(?:roll|pitch|yaw|twist|surge|sway|stroke)$", re.IGNORECASE,
)


VIDEO_EXTS = {
    "mp4", "mkv", "webm", "avi", "mov", "wmv", "flv", "m4v", "ts",
    "mpg", "mpeg", "m2ts", "vob", "ogv",
}
SCRIPT_EXTS = {"funscript", "syncscript"}


class FileKind(Enum):
    VIDEO = "video"
    SCRIPT = "script"
    OTHER = "other"


def kind_from_ext(ext: str) -> FileKind:
    e = ext.lower().lstrip(".")
    if e in SCRIPT_EXTS:
        return FileKind.SCRIPT
    if e in VIDEO_EXTS:
        return FileKind.VIDEO
    return FileKind.OTHER


def normalize(name: str) -> str:
    """Reduce a filename to a canonical comparison key.

    Strips:
      - file extension
      - axis suffix (.roll, .pitch, ...)
      - resolution / encoding tokens
      - version markers
      - separator characters and case

    Preserves:
      - leading author / release brackets (e.g. "(Author) Title.mp4")

    Why preserve author brackets? In Pixeldrain vaults, files like
      (Suppai) Compilation.mp4
      (Howlsfm) Compilation.mp4
    are usually different works that happen to share a generic title.
    Stripping the author would collapse them into one Pair and cause
    auto-rename to overwrite each other. Keeping the prefix means
    different authors → different Pairs (each in its own folder).
    Mirror-style uploads of the same content from multiple uploaders
    will also stay separate — slightly more folders, but no risk of
    cross-author script/video misalignment.
    """
    if not name:
        return ""
    base = name
    # Drop file extension(s) — handle .funscript on top of axis: foo.roll.funscript
    if "." in base:
        base = base.rsplit(".", 1)[0]
    # Drop axis suffix (.roll, etc.)
    base = AXIS_SUFFIX.sub("", base)
    # Pull the leading author bracket out so the generic ANY_BRACKETS
    # cleanup doesn't drop it. Reattach after.
    leading = ""
    m = LEADING_BRACKETS.match(base)
    if m:
        leading = m.group(0).strip()
        base = base[m.end():]
    # Inner bracketed content like " (1080p)" or "(v2)" gets stripped
    # — those are technical attributes, not author identity.
    base = ANY_BRACKETS.sub(" ", base)
    base = RESOLUTION_TOKENS.sub(" ", base)
    base = QUALITY_TOKENS.sub(" ", base)
    base = VERSION_SUFFIX.sub("", base)
    # Reattach the preserved author prefix
    if leading:
        base = leading + " " + base
    # Collapse separators AND drop punctuation (apostrophes, commas,
    # brackets, etc.) to nothing for comparison. This makes variants that
    # differ only in punctuation produce the same key — e.g. a source that
    # rewrites "Jane Doe's" to "Jane Doe s" must still match the original.
    base = re.sub(r"[\W_]+", "", base, flags=re.UNICODE)
    return base.lower().strip()


def _ratio(a: str, b: str) -> float:
    """Cheap similarity metric: longest common subsequence length / max length.

    We avoid pulling in difflib's overhead since this runs per-pair-candidate
    and the inputs are short normalized strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Prefer simple containment first — covers "ep1" ⊂ "episode1".
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    # Fall back to a difflib SequenceMatcher ratio for non-trivial cases.
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


class Confidence(Enum):
    HIGH = "high"      # exact normalized match
    MEDIUM = "medium"  # similar but not identical
    LOW = "low"        # orphan or ambiguous


@dataclass
class Candidate:
    """One file the user has selected. `key` is a stable identifier the
    caller uses to map back to the original picker row.

    `parent_path` (when supplied) lets the pairing algorithm prefer
    matches inside the same directory before considering cross-dir
    matches. Empty string means "directory unknown / flat list" and
    falls back to global matching."""
    key: object
    name: str
    kind: FileKind
    size: int = 0
    norm: str = ""
    parent_path: str = ""

    def __post_init__(self):
        if not self.norm:
            self.norm = normalize(self.name)


@dataclass
class Group:
    """A Pair-in-the-making produced by `pair_files`."""
    name: str                       # human-friendly group name
    videos: list[Candidate] = field(default_factory=list)
    scripts: list[Candidate] = field(default_factory=list)
    others: list[Candidate] = field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    note: str = ""                  # explanation shown in the preview

    @property
    def is_orphan(self) -> bool:
        """True when the group only has videos OR only has scripts.
        Orphan groups must skip auto-rename."""
        return not (self.videos and self.scripts)

    @property
    def total_files(self) -> int:
        return len(self.videos) + len(self.scripts) + len(self.others)


# Threshold above which a fuzzy match counts as "medium confidence".
# Below this we leave the file as an orphan.
FUZZY_MATCH_THRESHOLD = 0.78


def pair_files(candidates: list[Candidate]) -> list[Group]:
    """Group the given candidates into Pair-shaped groups.

    Algorithm (same-directory-first, see Step B notes):
      1. Bucket by (parent_path, normalized_base) — files only join a
         HIGH-confidence group if they live in the same directory.
      2. Cross-directory exact-name matches are MEDIUM (likely mirrors,
         but worth flagging in the preview).
      3. Same-directory fuzzy 1:1 matches are MEDIUM.
      4. Cross-directory fuzzy matches are NOT performed — too unsafe.
      5. Whatever is left becomes LOW-confidence orphans (one per file).

    `others` (non-video, non-script files) are always grouped alone as
    LOW-confidence orphans.
    """
    videos = [c for c in candidates if c.kind == FileKind.VIDEO]
    scripts = [c for c in candidates if c.kind == FileKind.SCRIPT]
    others = [c for c in candidates if c.kind == FileKind.OTHER]

    used_video_ids: set[int] = set()
    used_script_ids: set[int] = set()
    groups: list[Group] = []

    # ── Phase 1: exact match within the same directory ──
    same_dir: dict[tuple[str, str], dict[str, list[Candidate]]] = {}
    for v in videos:
        same_dir.setdefault((v.parent_path, v.norm), {"v": [], "s": []})["v"].append(v)
    for s in scripts:
        same_dir.setdefault((s.parent_path, s.norm), {"v": [], "s": []})["s"].append(s)

    for (parent, norm_key), bucket in same_dir.items():
        if bucket["v"] and bucket["s"]:
            name = bucket["v"][0].name.rsplit(".", 1)[0]
            groups.append(Group(
                name=name,
                videos=list(bucket["v"]),
                scripts=list(bucket["s"]),
                confidence=Confidence.HIGH,
                note="exact name match (same folder)",
            ))
            for c in bucket["v"]:
                used_video_ids.add(id(c))
            for c in bucket["s"]:
                used_script_ids.add(id(c))

    # ── Phase 1b: exact match across directories (likely mirror) ──
    cross_dir: dict[str, dict[str, list[Candidate]]] = {}
    for v in videos:
        if id(v) in used_video_ids:
            continue
        cross_dir.setdefault(v.norm, {"v": [], "s": []})["v"].append(v)
    for s in scripts:
        if id(s) in used_script_ids:
            continue
        cross_dir.setdefault(s.norm, {"v": [], "s": []})["s"].append(s)

    for norm_key, bucket in cross_dir.items():
        if bucket["v"] and bucket["s"]:
            # Only the unused ones reach here; flag MEDIUM because the
            # files live in different directories, which can mean either
            # mirror or unrelated coincidence.
            name = bucket["v"][0].name.rsplit(".", 1)[0]
            groups.append(Group(
                name=name,
                videos=list(bucket["v"]),
                scripts=list(bucket["s"]),
                confidence=Confidence.MEDIUM,
                note="exact name match across folders — verify before downloading",
            ))
            for c in bucket["v"]:
                used_video_ids.add(id(c))
            for c in bucket["s"]:
                used_script_ids.add(id(c))

    # ── Phase 2: same-directory fuzzy 1:1 matching ──
    rem_videos = [v for v in videos if id(v) not in used_video_ids]
    rem_scripts = [s for s in scripts if id(s) not in used_script_ids]

    pair_scores: list[tuple[float, Candidate, Candidate]] = []
    for v in rem_videos:
        for s in rem_scripts:
            # Fuzzy matches only inside the same directory. Cross-dir
            # fuzzy is too risky (different works often share name
            # fragments).
            if v.parent_path != s.parent_path:
                continue
            r = _ratio(v.norm, s.norm)
            if r >= FUZZY_MATCH_THRESHOLD:
                pair_scores.append((r, v, s))
    pair_scores.sort(key=lambda t: -t[0])

    matched_v: set[int] = set()
    matched_s: set[int] = set()
    for score, v, s in pair_scores:
        if id(v) in matched_v or id(s) in matched_s:
            continue
        matched_v.add(id(v))
        matched_s.add(id(s))
        groups.append(Group(
            name=v.name.rsplit(".", 1)[0],
            videos=[v], scripts=[s],
            confidence=Confidence.MEDIUM,
            note=f"fuzzy match in same folder ({score:.0%} similarity)",
        ))

    # ── Phase 3: orphans (one Pair per remaining file) ──
    for v in rem_videos:
        if id(v) in matched_v:
            continue
        groups.append(Group(
            name=v.name.rsplit(".", 1)[0],
            videos=[v],
            confidence=Confidence.LOW,
            note="no matching script found",
        ))
    for s in rem_scripts:
        if id(s) in matched_s:
            continue
        groups.append(Group(
            name=s.name.rsplit(".", 1)[0],
            scripts=[s],
            confidence=Confidence.LOW,
            note="no matching video found",
        ))
    for o in others:
        groups.append(Group(
            name=o.name.rsplit(".", 1)[0] if "." in o.name else o.name,
            others=[o],
            confidence=Confidence.LOW,
            note="non-video / non-script file",
        ))

    return groups
