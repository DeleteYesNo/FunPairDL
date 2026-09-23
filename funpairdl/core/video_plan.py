"""Which of a post's video links to download.

A post (and its comments) often carries several links to what is one work:
the same file on two hosts (mirror), the same video re-encoded smaller
(re-encode), the same animation with another character or outfit
(variant), and links to something else entirely. Downloading every link
wasted bandwidth and disk and left the user to untick by hand; this module
decides for them:

- mirrors and re-encodes of one video are ONE download — the pick follows
  the user's preference (smallest file that meets the resolution floor, or
  the best quality); the rest become fallback URLs tried only when the
  chosen one fails;
- variants are their own downloads (they land as ``<work> (<tag>).mp4``);
- a name that differs only by a version-ish word ("v2", "final", "(1)") is
  ambiguous — re-encode or variant? — and is returned for the user to
  decide, unless a preference settles it;
- comment links whose name matches no OP video are left alone;
- a link whose probe failed (unsupported site, 404) is never the pick while
  a live link to the same video exists, and a video with no live link at
  all is "dead" — nothing to download, the panel says so.

Everything is name- and probe-driven (filename, size, height, duration);
nothing is downloaded here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Words that describe an encode, not the content. Two names that differ
# only in these are the same video twice.
_ENCODE_TOKEN_RE = re.compile(
    r"^(?:\d{3,4}p|[248]k|uhd|fhd|qhd|hd|sd|h\.?26[45]|x26[45]|hevc|avc|av1|vp9|"
    r"small(?:er)?|compressed|comp|re-?encoded?|reenc|lite|low|mini|tiny|web|"
    r"\d+(?:\.\d+)?[mg]b|\d{2,3}fps|hq|lq|mq|high|medium|med|original|orig|source|src|"
    r"bitrate|crf\d*|q\d{1,2}|\d{3,4}x\d{3,4}|mp4|mkv|webm|mov)$",
    re.IGNORECASE,
)
# Version-ish words: they tell two files apart but not HOW they differ.
_OPAQUE_TOKEN_RE = re.compile(
    r"^(?:\d{1,2}|v\d+|ver\d*|version\d*|final|fix(?:ed)?|new|old|updated?|upd|alt|"
    r"copy|edit(?:ed)?|re-?up(?:load)?|mirror|link|video|file|download|dl)$",
    re.IGNORECASE,
)
# Random ids (a pixeldrain/iwara token in brackets) say nothing either way.
_ID_TOKEN_RE = re.compile(r"^(?=.*\d)[a-z0-9_-]{8,}$", re.IGNORECASE)
_SITE_PREFIX_RE = re.compile(
    r"^\s*(?:iwara|source(?:\s*video)?|mirror|video|rule34video|pixeldrain|mega)\s*[-—–:]\s*",
    re.IGNORECASE,
)
_RES_IN_NAME_RE = re.compile(r"(?<![a-z0-9])(\d{3,4})p(?![a-z0-9])|(?<![a-z0-9])([248])k(?![a-z0-9])", re.IGNORECASE)

_RES_ORDER = {"best": 10 ** 6, "2160": 2160, "1080": 1080, "720": 720, "480": 480, "360": 360}


@dataclass
class VideoSpec:
    url: str
    name: str = ""
    source: str = "OP"           # "OP" | "comment"
    size: int = 0
    height: int = 0              # best known height (yt-dlp formats) or 0
    duration: float = 0.0
    priority: float = 99.0       # host priority from the panel (lower = better)
    failed: bool = False         # its probe failed: unsupported site, gone
    key: str = ""                # filled in: mirror key
    stem: str = ""               # filled in: comparison stem
    tokens: set[str] = field(default_factory=set)


@dataclass
class Prefs:
    pick_mode: str = "smallest"        # "smallest" | "best_quality"
    min_resolution: str = "1080"       # floor; "best" = only the top height qualifies
    encode_vs_variant: str = "ask"     # "ask" | "reencode" | "variant"


def _stem_of(spec: VideoSpec) -> str:
    """The name to compare: the probed filename, else the URL's descriptive
    slug (rule34video/iwara), else the URL's last segment."""
    from funpairdl.core.queue_manager import QueueManager
    from funpairdl.core.pair import FileType, PairItem
    name = (spec.name or "").strip()
    if name:
        # The probed title/filename keeps its brackets ("(4K/Nude) Work
        # [Auth]"), which is what tells a variant tag from the work's name;
        # a URL slug flattens them ("4k-nude-work-auth").
        ident = name
    else:
        item = PairItem(url=spec.url, filename=QueueManager._guess_filename(spec.url, "video"),
                        file_type=FileType.VIDEO)
        ident = QueueManager._video_identity(item)
    s = Path(ident).stem if re.search(r"\.[a-z0-9]{2,4}$", ident, re.IGNORECASE) else ident
    return _SITE_PREFIX_RE.sub("", s).strip()


def _tokens(stem: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t}


_BRACKET_RE = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")


def _core_tokens(stem: str) -> frozenset[str]:
    """The words that name the work: bracketed tags (an outfit, an id), encode
    words and version words dropped. "Work Title (nude) [1080p] v2" and
    "Work Title" agree on {"work", "title"}."""
    bare = _BRACKET_RE.sub(" ", stem or "")
    return frozenset(t for t in _tokens(bare)
                     if not _ENCODE_TOKEN_RE.match(t) and not _OPAQUE_TOKEN_RE.match(t)
                     and not _ID_TOKEN_RE.match(t))


def _height_from_name(stem: str) -> int:
    m = _RES_IN_NAME_RE.search(stem or "")
    if not m:
        return 0
    if m.group(1):
        return int(m.group(1))
    return {"2": 1440, "4": 2160, "8": 4320}.get(m.group(2), 0)


def _duration_differs(a: float, b: float) -> bool:
    if not a or not b:
        return False
    return abs(a - b) > max(3.0, 0.02 * max(a, b))


def _classify(spec: VideoSpec, ref: VideoSpec,
              credits: frozenset[str] = frozenset()) -> tuple[str, frozenset[str], str]:
    """(kind, cluster key, tag) of `spec` relative to `ref`:
    kind = "mirror" | "reencode" | "ambiguous" | "variant".
    `credits` are the post's creator words: a host title that adds
    "[Creator]" to the file's name is the same video, not a variant."""
    from funpairdl.core.queue_manager import QueueManager
    if _duration_differs(spec.duration, ref.duration):
        tag = QueueManager._variant_tag(spec.stem + ".mp4", ref.stem + ".mp4") or f"{int(round(spec.duration))}s"
        return "variant", frozenset({"__dur__", str(int(round(spec.duration)))}), tag
    diff = (spec.tokens - ref.tokens) | (ref.tokens - spec.tokens)
    diff = {t for t in diff if not _ID_TOKEN_RE.match(t) and t not in credits}
    if not diff:
        return "mirror", frozenset(), ""
    variant = {t for t in diff if not _ENCODE_TOKEN_RE.match(t) and not _OPAQUE_TOKEN_RE.match(t)}
    if variant:
        tag = QueueManager._variant_tag(spec.stem + ".mp4", ref.stem + ".mp4")
        if not tag:
            tag = " ".join(sorted(t for t in (spec.tokens - ref.tokens) if t in variant)) or " ".join(sorted(variant))
        return "variant", frozenset(sorted(variant)), tag
    if all(_ENCODE_TOKEN_RE.match(t) for t in diff):
        return "reencode", frozenset(), ""
    tag = QueueManager._variant_tag(spec.stem + ".mp4", ref.stem + ".mp4") or " ".join(sorted(diff))
    return "ambiguous", frozenset(), tag


def _qualifies(spec: VideoSpec, floor: str, top_height: int) -> bool:
    h = spec.height or _height_from_name(spec.stem)
    if floor == "best":
        return not h or not top_height or h >= top_height
    need = _RES_ORDER.get(str(floor), 0)
    return not h or h >= need


def _order(cands: list[VideoSpec], prefs: Prefs, pending: set[str] | None = None) -> list[VideoSpec]:
    """Best pick first. A dead link (failed probe) is never the pick while a
    live one exists; an unanswered ambiguity (`pending`) is never the pick —
    it rides last as a fallback until the user says what it is."""
    pending = pending or set()
    heights = [s.height or _height_from_name(s.stem) for s in cands]
    top = max(heights) if heights else 0

    def h_of(s: VideoSpec) -> int:
        return s.height or _height_from_name(s.stem)

    if prefs.pick_mode == "best_quality":
        def k(s: VideoSpec):
            return (1 if s.failed else 0, 1 if s.url in pending else 0,
                    0 if _qualifies(s, prefs.min_resolution, top) else 1,
                    -h_of(s), -(s.size or 0), 0 if s.source == "OP" else 1, s.priority)
    else:
        def k(s: VideoSpec):
            return (1 if s.failed else 0, 1 if s.url in pending else 0,
                    0 if _qualifies(s, prefs.min_resolution, top) else 1,
                    0 if s.size else 1, s.size or 0, 0 if s.source == "OP" else 1, s.priority)
    return sorted(cands, key=k)


def plan_videos(videos: list[VideoSpec], prefs: Prefs | None = None,
                decisions: dict[str, str] | None = None,
                credits: list[str] | None = None) -> dict:
    """Group a post's video links and pick what to download.

    Returns::

        {"groups": [{"key", "kind": "primary"|"variant"|"unrelated", "tag",
                     "chosen": url, "alternates": [url], "members": {url: role},
                     "reason": str}],
         "ambiguous": [{"url", "ref", "tag", "default": "reencode"}],
         "roles": {url: "chosen"|"alternate"|"variant"|"ambiguous"|"unrelated"|"dead"}}

    `decisions` = {url: "reencode" | "variant"} answers a previous call's
    ambiguities. With ``encode_vs_variant`` set, they are answered that way.
    `credits` = the post's creator names (the title's "[Creator]" prefix, the
    OP): words a host adds to a title that don't make another video.
    A video whose every link failed its probe is "dead": its group has no
    chosen link and every member's role is "dead".
    """
    from funpairdl.core.queue_manager import QueueManager
    prefs = prefs or Prefs()
    decisions = decisions or {}
    credit_words = frozenset(t for c in (credits or []) for t in _tokens(c) if len(t) >= 3)
    for v in videos:
        v.stem = _stem_of(v)
        v.tokens = _tokens(v.stem)

    # OP links name the post's work(s); a comment link belongs to the work
    # whose name its own name contains ("Work Title Mockgan" -> "Work Title").
    cores = {v.url: _core_tokens(v.stem) for v in videos}
    raw_cores: list[frozenset[str]] = []
    for v in videos:
        if v.source == "OP" and cores[v.url] not in raw_cores:
            raw_cores.append(cores[v.url])
    if not raw_cores:  # comment-only post: each distinct name is a work
        for v in videos:
            if cores[v.url] not in raw_cores:
                raw_cores.append(cores[v.url])
    # A core that contains another (2+ words) names the same work with a
    # word more — a slug keeps the "[HMV]" tag a title drops. Fold it in.
    op_cores: list[frozenset[str]] = []
    alias: dict[frozenset[str], frozenset[str]] = {}
    for core in sorted(raw_cores, key=len):
        base = next((k for k in op_cores if len(k) >= 2 and k <= core), None)
        if base is not None:
            alias[core] = base
        else:
            op_cores.append(core)
    for u, core in cores.items():
        cores[u] = alias.get(core, core)

    # A numbered series ("Diary 01" … "04") is several works that share
    # a name: when the OP links under one core carry different numbers, the
    # number is part of the key. A lone number ("Work" vs "Work 2") stays an
    # open question below.
    def _num_sig(v: VideoSpec) -> str:
        return " ".join(sorted(t for t in v.tokens if re.fullmatch(r"\d{1,2}", t)))

    series: set[frozenset[str]] = set()
    for core in op_cores:
        sigs = {_num_sig(v) for v in videos if v.source == "OP" and cores[v.url] == core}
        if not any(v.source == "OP" for v in videos):
            sigs = {_num_sig(v) for v in videos if cores[v.url] == core}
        if len(sigs - {""}) >= 2:
            series.add(core)

    def _key_of(v: VideoSpec) -> str:
        core = cores[v.url]
        if core in op_cores:
            key = " ".join(sorted(core))
        else:
            fits = [k for k in op_cores if k and k <= core]
            if not fits:
                return "?" + " ".join(sorted(core))
            core = max(fits, key=len)
            key = " ".join(sorted(core))
        if core in series and _num_sig(v):
            key += " #" + _num_sig(v)
        return key

    for v in videos:
        v.key = _key_of(v)
    op_keys = [v.key for v in videos if v.source == "OP"] or [
        " ".join(sorted(k)) for k in op_cores]
    by_key: dict[str, list[VideoSpec]] = {}
    for v in videos:
        by_key.setdefault(v.key, []).append(v)

    groups: list[dict] = []
    ambiguous: list[dict] = []
    roles: dict[str, str] = {}

    for key, members in by_key.items():
        related = (not op_keys) or (key in op_keys)
        if not related:
            for v in members:
                roles[v.url] = "unrelated"
            groups.append({"key": key, "kind": "unrelated", "tag": "", "chosen": "",
                           "alternates": [], "members": {v.url: "unrelated" for v in members},
                           "reason": "留言的影片與帖子的作品名稱對不上"})
            continue
        # Reference: the live OP link with the shortest name (fewest qualifiers).
        ops = [v for v in members if v.source == "OP"] or members
        ref = min(ops, key=lambda v: (v.failed, len(v.tokens), v.priority))
        clusters: dict[frozenset, dict] = {}
        for v in members:
            if v is ref:
                kind, ckey, tag = "mirror", frozenset(), ""
            else:
                kind, ckey, tag = _classify(v, ref, credit_words)
            if kind == "ambiguous" and (v.failed or ref.failed):
                # Nothing to ask about a link that can't be downloaded: it
                # only ever serves as a fallback.
                kind, ckey = "reencode", frozenset()
            if kind == "ambiguous":
                choice = decisions.get(v.url) or (
                    prefs.encode_vs_variant if prefs.encode_vs_variant in ("reencode", "variant") else "")
                if choice == "variant":
                    kind, ckey = "variant", frozenset({"__ask__", v.url})
                elif choice == "reencode":
                    kind, ckey = "reencode", frozenset()
                else:
                    ambiguous.append({"url": v.url, "ref": ref.url, "tag": tag, "default": "reencode"})
                    kind, ckey = "reencode", frozenset()
                    roles[v.url] = "ambiguous"
            c = clusters.setdefault(ckey, {"members": [], "tag": tag, "kinds": {}})
            c["members"].append(v)
            c["kinds"][v.url] = kind
            if tag and not c["tag"]:
                c["tag"] = tag

        for ckey, c in clusters.items():
            ordered = _order(c["members"], prefs,
                             {u for u, r in roles.items() if r == "ambiguous"})
            chosen, rest = ordered[0], ordered[1:]
            is_primary = ckey == frozenset()
            if chosen.failed:
                # The best link is dead only when every link is.
                for v in c["members"]:
                    roles[v.url] = "dead"
                groups.append({
                    "key": key, "kind": "primary" if is_primary else "variant",
                    "tag": "" if is_primary else c["tag"], "dead": True,
                    "chosen": "", "alternates": [],
                    "members": {v.url: "dead" for v in c["members"]},
                    "reason": "所有連結都無法下載",
                })
                continue
            member_roles = {}
            for v in c["members"]:
                if v is chosen:
                    member_roles[v.url] = "chosen"
                else:
                    member_roles[v.url] = c["kinds"].get(v.url, "mirror")
                    if member_roles[v.url] == "variant":
                        member_roles[v.url] = "mirror"  # a mirror of this variant
            for v in c["members"]:
                if roles.get(v.url) == "ambiguous":
                    continue
                if v is chosen:
                    roles[v.url] = "chosen" if is_primary else "variant"
                else:
                    roles[v.url] = "alternate"
            if is_primary:
                reason = _pick_reason(chosen, rest, prefs)
            else:
                reason = f"同一作品的變體 ({c['tag']})" if c["tag"] else "同一作品的變體"
            groups.append({
                "key": key, "kind": "primary" if is_primary else "variant",
                "tag": "" if is_primary else c["tag"],
                "chosen": chosen.url, "alternates": [v.url for v in rest],
                "members": member_roles, "reason": reason,
            })

    return {"groups": groups, "ambiguous": ambiguous, "roles": roles}


def _pick_reason(chosen: VideoSpec, rest: list[VideoSpec], prefs: Prefs) -> str:
    if not rest:
        return ""
    how = "最小的檔" if prefs.pick_mode != "best_quality" else "畫質最高的"
    floor = "" if prefs.min_resolution == "best" else f"，至少 {prefs.min_resolution}p"
    return f"{len(rest) + 1} 個來源是同一影片：選{how}{floor}，其餘做失敗備援"
