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
    r"small(?:er)?|(?:un)?compressed|comp|re-?encoded?|reenc|lite|low|mini|tiny|web|rf\d{1,2}|"
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


def _is_id(t: str) -> bool:
    """A random id ("gu6L8LWb", "a1b2c3d4"), not a word with a digit in it
    ("FortyTwo3D", "Studio2025"): ids are unpronounceable or digit-riddled."""
    if not _ID_TOKEN_RE.match(t):
        return False
    letters = re.sub(r"[^a-z]", "", t.lower())
    runs = len(re.findall(r"\d+", t))
    digits = sum(c.isdigit() for c in t)
    if runs >= 2 or not letters:
        return True
    if digits >= 3 and not re.search(r"\d$", t):
        return True  # "abc123de"; a year or number at the end is a word's ("Studio2025")
    return sum(c in "aeiouy" for c in letters) / len(letters) < 0.25
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
    pack: str = ""               # the folder/list link this file came in ("" = a plain link)
    width: int = 0               # frame size when a probe read it (0 = unknown)
    fmt: str = ""                # filled in: "flat" | "vr" | "passthrough" | "" (unknown)
    key: str = ""                # filled in: mirror key
    stem: str = ""               # filled in: comparison stem
    tokens: set[str] = field(default_factory=set)


@dataclass
class Prefs:
    pick_mode: str = "smallest"        # "smallest" | "best_quality"
    min_resolution: str = "1080"       # floor; "best" = only the top height qualifies
    encode_vs_variant: str = "ask"     # "ask" | "reencode" | "variant"
    vr_versions: str = "flat"          # one video in 2D and VR: "flat" | "vr" | "all"


_VR_NAME_RE = re.compile(
    r"(?<![a-z0-9])(?:vr|vr180|vr360|sbs|3dh|fisheye\d*|mkx\d+|180x180|lr[-_ ]?180|180[-_ ]?(?:lr|sbs))(?![a-z0-9])",
    re.IGNORECASE)
_PASSTHROUGH_RE = re.compile(r"pass[-_ ]?through", re.IGNORECASE)
FORMAT_LABEL = {"flat": "2D", "vr": "VR", "passthrough": "Passthrough"}


def video_format(width: int, height: int, name: str = "") -> str:
    """How a video is meant to be watched: "flat" (2D), "vr" (180° side by
    side or over-under), "passthrough" (VR with a keyed background for
    mixed reality), "" when nothing tells. The frame decides when known —
    a 2:1 or square frame of VR size; a name only when it is not."""
    name = name or ""
    if width and height:
        r = width / height
        vr = (1.95 <= r <= 2.05 and height >= 1440) or (0.98 <= r <= 1.02 and width >= 2880)
    elif _VR_NAME_RE.search(name) or _PASSTHROUGH_RE.search(name):
        vr = True
    else:
        return ""
    if not vr:
        return "flat"
    return "passthrough" if _PASSTHROUGH_RE.search(name) else "vr"


def _stem_of(spec: VideoSpec) -> str:
    """The name to compare: the probed filename, else the URL's descriptive
    slug (rule34video/iwara), else the URL's last segment."""
    from funpairdl.core.queue_manager import QueueManager
    from funpairdl.core.pair import FileType, PairItem
    name = (spec.name or "").strip()
    bare = Path(name).stem if re.search(r"\.[a-z0-9]{2,4}$", name, re.IGNORECASE) else name
    segs = {x.lower() for x in re.split(r"[/?&=#]", spec.url or "") if x}
    if name and (not _core_tokens(_SITE_PREFIX_RE.sub("", bare)) or bare.lower() in segs):
        name = ""  # the host titled it by its id ("7abcd"): the URL's slug says more
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


# Spellings of one word, folded so "NO WM" and "no watermark" agree.
_SYNONYMS = {"watermark": "wm", "watermarks": "wm", "watermarked": "wm", "watermarkless": "wm"}


_GLUED_ENCODE_RE = re.compile(r"(?<=[a-z])(\d{2,3}fps|\d{3,4}p)(?![a-z0-9])")


def _tokens(stem: str) -> set[str]:
    low = _GLUED_ENCODE_RE.sub(r" \1", (stem or "").lower())
    return {_SYNONYMS.get(t, t) for t in re.split(r"[^a-z0-9]+", low) if t}


# A release year ("Work 2020") dates a work; one host keeps it, the next drops it.
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_BRACKET_RE = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")


def _core_tokens(stem: str) -> frozenset[str]:
    """The words that name the work: bracketed tags (an outfit, an id), encode
    words and version words dropped. "Work Title (nude) [1080p] v2" and
    "Work Title" agree on {"work", "title"}."""
    bare = _BRACKET_RE.sub(" ", stem or "")
    return frozenset(t for t in _tokens(bare)
                     if not _ENCODE_TOKEN_RE.match(t) and not _OPAQUE_TOKEN_RE.match(t)
                     and not _is_id(t) and not _YEAR_RE.match(t))


_SQUARE_RE = re.compile(r"[\[【]([^\]】]*)[\]】]")
# Genre/format tags that sit in square brackets but name no creator.
_TAG_WORDS = frozenset({"hmv", "pmv", "sfm", "vr", "3d", "2d", "sound", "audio", "voice",
                        "remake", "free", "full", "ai", "loop", "joi", "cei", "fap", "hero",
                        "fh", "multi", "axis", "script", "funscript", "no", "wm", "eng", "sub",
                        "subs", "uncensored", "censored"})


def _creators(stem: str) -> frozenset[str]:
    """Who made it: the words in a name's square brackets ("[AB12] Work",
    "Work [FortyTwo3D]") that are not format tags or encode words."""
    out = set()
    for inner in _SQUARE_RE.findall(stem or ""):
        for t in _tokens(inner):
            if (t in _TAG_WORDS or _ENCODE_TOKEN_RE.match(t) or _OPAQUE_TOKEN_RE.match(t)
                    or _is_id(t)):
                continue
            out.add(t)
    return frozenset(out)


def _pack_key(v: "VideoSpec", core: frozenset[str]) -> str:
    """One pack file's work: its name's core words, its creators, its
    numbers and parenthesised tags — so "Work (1-3) uncompressed.mp4" and
    "Work (1-3).mp4" are one video, while "[AB12] Heroine Nova" and
    "[FortyTwo3D] Heroine Nova", "Diary 01" and "Diary 02", "Work (nude)" and
    "Work (stockings)" are not."""
    nums = {t for t in v.tokens if re.fullmatch(r"\d{1,3}", t)}
    tags = {t for inner in _PAREN_RE.findall(v.stem or "") for t in _tokens(inner)
            if not _ENCODE_TOKEN_RE.match(t) and not _is_id(t)}
    return "pack:" + " ".join(sorted(set(core) | _creators(v.stem) | nums | tags))


def _pack_anchor(v: "VideoSpec", anchors: list["VideoSpec"], cores: dict) -> "VideoSpec | None":
    """The pack file a plain link is another copy of: their core names
    contain one another (2+ shared words) and their creators don't
    conflict; the anchor whose creator the link names wins, then the
    closest name. None when nothing fits or two fit equally."""
    vc, vcr = cores[v.url], _creators(v.stem)
    best: list[tuple[float, VideoSpec]] = []
    for a in anchors:
        ac, acr = cores[a.url], _creators(a.stem)
        if not ac or not vc or not (ac <= vc or vc <= ac) or len(ac & vc) < 2:
            continue
        if acr and vcr and not (acr & vcr):
            continue  # "[AB12] Heroine Nova" is not "[FortyTwo3D] Heroine Nova"
        # The link names the anchor's creator (or the anchor, a slug with
        # no brackets, carries the link's): that settles a tie.
        named = (acr and acr <= v.tokens) or (vcr and vcr <= a.tokens)
        score = len(ac & vc) / len(ac | vc) + (1.0 if named else 0.0)
        best.append((score, a))
    if not best:
        return None
    best.sort(key=lambda x: -x[0])
    if len(best) > 1 and abs(best[0][0] - best[1][0]) < 1e-9 and best[0][1].key != best[1][1].key:
        return None
    return best[0][1]


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
    if spec.fmt and ref.fmt and spec.fmt != ref.fmt:
        # The same video rendered for another way of watching: its own
        # download, kept or left per the vr_versions setting.
        return "variant", frozenset({"__fmt__", spec.fmt}), FORMAT_LABEL[spec.fmt]
    if _duration_differs(spec.duration, ref.duration):
        tag = QueueManager._variant_tag(spec.stem + ".mp4", ref.stem + ".mp4") or f"{int(round(spec.duration))}s"
        return "variant", frozenset({"__dur__", str(int(round(spec.duration)))}), tag
    if (spec.duration >= 60 and ref.duration >= 60 and abs(spec.duration - ref.duration) <= 0.5):
        a, b = _core_tokens(spec.stem), _core_tokens(ref.stem)
        if a | b and len(a & b) / len(a | b) < 0.34:
            # One exact length under two unrelated host titles ("Booru - If
            # it exists…" / "Clip (Sound) Booru Video #…"): one video.
            # (Character alts share most of their name and stay variants.)
            return "mirror", frozenset(), ""
    diff = (spec.tokens - ref.tokens) | (ref.tokens - spec.tokens)
    diff = {t for t in diff if not _is_id(t) and t not in credits}
    if any(re.fullmatch(r"c?rf\d*", t) for t in diff):
        # HandBrake's "-P4-RF35": the preset number rides with the quality.
        diff = {t for t in diff if not re.fullmatch(r"p\d", t)}
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


_PAREN_RE = re.compile(r"[\(（]([^\)）]*)[\)）]")


def _paren_diff(spec: VideoSpec, ref: VideoSpec, credits: frozenset[str] = frozenset()) -> set[str]:
    """Words of a parenthesised tag ("(nude)", "(Pip alt)") one name has
    and the other lacks entirely."""
    tags = lambda st: {t for inner in _PAREN_RE.findall(st or "") for t in _tokens(inner)}
    diff = ((tags(spec.stem) - ref.tokens) | (tags(ref.stem) - spec.tokens)) - credits
    return {t for t in diff if not _ENCODE_TOKEN_RE.match(t) and not _OPAQUE_TOKEN_RE.match(t)
            and not _is_id(t)}


def _classify_in_pack(spec: VideoSpec, ref: VideoSpec,
                      credits: frozenset[str] = frozenset()) -> tuple[str, frozenset[str], str]:
    """A plain link matched to a pack file is another copy of it — hosts
    retitle freely ("[Studio][4K] Work Full Animation" for "[Studio] Work.mp4")
    — unless its length differs or a parenthesised tag sets it apart
    ("Work (nude)")."""
    from funpairdl.core.queue_manager import QueueManager
    if _duration_differs(spec.duration, ref.duration) or (spec.fmt and ref.fmt and spec.fmt != ref.fmt):
        return _classify(spec, ref, credits)
    # A tag the other name carries anywhere is no difference: a slug
    # ("work-ember-alt-scene-3.mp4") keeps the words, not the brackets.
    diff = _paren_diff(spec, ref, credits)
    if diff:
        tag = QueueManager._variant_tag(spec.stem + ".mp4", ref.stem + ".mp4") or " ".join(sorted(diff))
        return "variant", frozenset(sorted(diff)), tag
    return "mirror", frozenset(), ""


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
        v.fmt = video_format(v.width, v.height, v.name or v.stem)

    # OP links name the post's work(s); a comment link belongs to the work
    # whose name its own name contains ("Work Title Mockgan" -> "Work Title").
    cores = {v.url: _core_tokens(v.stem) for v in videos}
    own_cores = dict(cores)  # before aliasing folds "Heroine Nova" into every "... Heroine Nova"
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
                # Or the other way round: "Garden Party [Artist]" under
                # "Stream-Vid: Garden Party" (half the OP's words at least).
                fits = [k for k in op_cores if len(core) >= 2 and core <= k and 2 * len(core) >= len(k)]
            if not fits:
                return "?" + " ".join(sorted(core))
            core = max(fits, key=len)
            key = " ".join(sorted(core))
        if core in series and _num_sig(v):
            key += " #" + _num_sig(v)
        return key

    for v in videos:
        v.key = _key_of(v)

    # A pack (folder/list) holds the post's files by their real names: each
    # distinct file is its own video, and a plain link is another copy of
    # the pack file it names. Name cores alone merged a scripter's
    # collection of eleven "Heroine Nova" animations by eleven creators into
    # one work.
    anchors = [v for v in videos if v.pack]
    for a in anchors:
        a.key = _pack_key(a, own_cores[a.url])
    for v in videos:
        if not v.pack and anchors:
            a = _pack_anchor(v, anchors, own_cores)
            if a is not None:
                v.key = a.key

    # Two links of one exact length (a minute or more, within half a
    # second) are one video, whatever the hosts call them.
    by_len: dict[str, float] = {}
    for v in videos:
        if v.duration and v.duration >= 60 and not v.failed:
            by_len.setdefault(v.key, v.duration)
    for k1 in list(by_len):
        for k2 in list(by_len):
            if k1 < k2 and k1 in by_len and k2 in by_len and abs(by_len[k1] - by_len[k2]) <= 0.5:
                if k1.startswith("pack:") and k2.startswith("pack:"):
                    continue  # two files in packs are two videos
                keep, drop = (k2, k1) if k2.startswith("pack:") else (k1, k2)
                for v in videos:
                    if v.key == drop:
                        v.key = keep
                by_len.pop(drop, None)

    # A post of comment links only: each of them names a work.
    op_keys = [v.key for v in videos if v.source == "OP"] or [v.key for v in videos]
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
        # Reference: the live OP link with the shortest name (fewest qualifiers);
        # in a pack's group, the pack file.
        live = [v for v in members if not v.failed] or members
        in_pack = key.startswith("pack:")
        fmt_rank = _FORMAT_ORDER.get(prefs.vr_versions, _FORMAT_ORDER["flat"])
        # The render the setting prefers is the work's own; then the pack
        # file (in a pack's group) or an OP link; then the plainest name.
        ref = min(live, key=lambda v: (
            fmt_rank.index(v.fmt or "flat"),
            0 if ((v.pack if in_pack else v.source == "OP") or
                  not any((o.pack if in_pack else o.source == "OP") for o in live)) else 1,
            len(v.tokens), v.priority))
        clusters: dict[frozenset, dict] = {}
        for v in members:
            if v is ref:
                kind, ckey, tag = "mirror", frozenset(), ""
            elif key.startswith("pack:"):
                kind, ckey, tag = _classify_in_pack(v, ref, credit_words)
            else:
                kind, ckey, tag = _classify(v, ref, credit_words)
            if (kind == "variant" and v.failed and not ref.failed and "__dur__" not in ckey
                    and not _paren_diff(v, ref, credit_words)):
                # A dead link titled a little differently ("Stream-Vid: Work"
                # beside the live "Work [Artist]") is a dead copy, not a lost
                # version — only a bracketed tag or another length says so.
                kind, ckey, tag = "reencode", frozenset(), ""
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

    _apply_format_pref(groups, roles, {v.url: v for v in videos}, cores, prefs)
    return {"groups": groups, "ambiguous": ambiguous, "roles": roles}


_FORMAT_ORDER = {"flat": ("flat", "vr", "passthrough"), "vr": ("vr", "passthrough", "flat")}


def _apply_format_pref(groups: list[dict], roles: dict, specs: dict, cores: dict, prefs: Prefs) -> None:
    """One video offered as 2D and as VR (and passthrough) — the same work,
    the same length — keeps one way of watching: the 2D render by default,
    the VR one, or all of them (``vr_versions``). The others become role
    "format": left out, a click away."""
    order = _FORMAT_ORDER.get(prefs.vr_versions) or _FORMAT_ORDER["flat"]
    keep_all = prefs.vr_versions not in _FORMAT_ORDER
    live = [g for g in groups if g.get("chosen") and g["kind"] in ("primary", "variant")]
    fmt = {id(g): specs[g["chosen"]].fmt or "flat" for g in live}
    if len({fmt[id(g)] for g in live}) < 2:
        return

    def same_media(a: dict, b: dict) -> bool:
        sa, sb = specs[a["chosen"]], specs[b["chosen"]]
        if sa.duration and sb.duration:
            if abs(sa.duration - sb.duration) > 1.0:
                return False
            ca, cb = cores.get(sa.url, frozenset()), cores.get(sb.url, frozenset())
            return a["key"] == b["key"] or len(ca & cb) >= 2
        return a["key"] == b["key"]

    parent = {id(g): id(g) for g in live}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if fmt[id(a)] != fmt[id(b)] and same_media(a, b):
                parent[find(id(a))] = find(id(b))
    sets: dict[int, list[dict]] = {}
    for g in live:
        sets.setdefault(find(id(g)), []).append(g)
    for members in sets.values():
        present = {fmt[id(g)] for g in members}
        if len(present) < 2:
            continue
        keep = next(f for f in order if f in present)
        if keep_all:
            # Every render goes; the preferred one is the work, the others
            # its "(VR)" / "(Passthrough)" variants.
            for g in members:
                if fmt[id(g)] != keep and g["kind"] == "primary":
                    g.update(kind="variant", tag=FORMAT_LABEL[fmt[id(g)]],
                             reason=f"同一影片的 {FORMAT_LABEL[fmt[id(g)]]} 版")
                    roles[g["chosen"]] = "variant"
            continue
        lost_primary = any(g["kind"] == "primary" and fmt[id(g)] != keep for g in members)
        for g in members:
            if fmt[id(g)] == keep:
                if lost_primary and g["kind"] == "variant" and g["tag"] == FORMAT_LABEL[keep]:
                    # The kept render IS the work now, not a "2D variant" of it.
                    g.update(kind="primary", tag="", reason="")
                    roles[g["chosen"]] = "chosen"
                    g["members"][g["chosen"]] = "chosen"
                continue
            label = FORMAT_LABEL[fmt[id(g)]]
            g.update(kind="format", tag=label, chosen="", alternates=[],
                     members={u: "format" for u in g["members"]},
                     reason=f"同一影片的 {label} 版；設定只下載 {FORMAT_LABEL[keep]} 版")
            for u in g["members"]:
                roles[u] = "format"


def _pick_reason(chosen: VideoSpec, rest: list[VideoSpec], prefs: Prefs) -> str:
    if not rest:
        return ""
    how = "最小的檔" if prefs.pick_mode != "best_quality" else "畫質最高的"
    floor = "" if prefs.min_resolution == "best" else f"，至少 {prefs.min_resolution}p"
    return f"{len(rest) + 1} 個來源是同一影片：選{how}{floor}，其餘做失敗備援"
