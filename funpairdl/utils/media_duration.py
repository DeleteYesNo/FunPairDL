"""Media/script durations without downloading whole files.

Duration is the one signal that survives when names and tags say nothing:
a script's last action lands within seconds of its video's end, and a
work's multi-axis scripts all share one length. This module reads it:

* funscript — the last action timestamp (``metadata.duration`` when the
  author's tool wrote one), plus the metadata fields that can name the
  video outright (``video_url``, ``title``).
* mp4/mov/m4v — the ``mvhd`` atom: parsed from the head when the file is
  "faststart", else located after ``mdat`` with one or two ranged reads.
* webm/mkv — the Segment Info element (Duration × TimecodeScale), which
  sits in the first few KB.

The parsers are pure (bytes in, numbers out) so they are unit-testable; the
network driver ``probe_media_duration`` does the ranged GETs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any

import aiohttp

logger = logging.getLogger("funpairdl.utils.media_duration")

HEAD_BYTES = 256 * 1024
MOOV_CAP = 16 * 1024 * 1024        # never pull a moov bigger than this
MAX_HOPS = 4                       # atom headers to chase past mdat/free

VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv")


# ---------------------------------------------------------------------------
# funscript
# ---------------------------------------------------------------------------

def funscript_info(data: bytes) -> dict[str, Any]:
    """{"duration": seconds|None, "title": str, "video_url": str} from a
    funscript's bytes. Duration = last action (ms) → s; metadata.duration
    is used only when the actions agree with it (some tools write junk)."""
    out: dict[str, Any] = {"duration": None, "title": "", "video_url": ""}
    try:
        doc = json.loads(data.decode("utf-8", "ignore"))
    except Exception:
        return out
    if not isinstance(doc, dict):
        return out
    actions = doc.get("actions")
    last_ms = 0
    if isinstance(actions, list):
        for a in actions:
            try:
                at = int(a.get("at", 0))
            except Exception:
                continue
            if at > last_ms:
                last_ms = at
    if last_ms > 0:
        out["duration"] = last_ms / 1000.0
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        try:
            md = float(meta.get("duration") or 0)
        except Exception:
            md = 0.0
        # Trust metadata.duration when it is at least the action span and
        # not absurdly longer (a video with a long tail after the last beat).
        if md > 0 and out["duration"] and md >= out["duration"] and md <= out["duration"] * 1.5 + 60:
            out["duration"] = md
        elif md > 0 and not out["duration"]:
            out["duration"] = md
        out["title"] = str(meta.get("title") or "").strip()
        out["video_url"] = str(meta.get("video_url") or "").strip()
    return out


# ---------------------------------------------------------------------------
# mp4 (ISO BMFF)
# ---------------------------------------------------------------------------

def _atoms(buf: bytes, start: int = 0):
    """Yield (offset, type, size|None, header_len) for top-level atoms whose
    header lies inside `buf`. size None = "extends to end of file"."""
    off = start
    while off + 8 <= len(buf):
        size = int.from_bytes(buf[off:off + 4], "big")
        typ = buf[off + 4:off + 8]
        hdr = 8
        if size == 1:
            if off + 16 > len(buf):
                return
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            hdr = 16
        elif size == 0:
            yield off, typ, None, hdr
            return
        yield off, typ, size, hdr
        if size < hdr:
            return
        off += size


def mp4_duration_from_moov(moov: bytes) -> float | None:
    """Seconds from a complete ``moov`` atom (header included)."""
    if len(moov) < 8 or moov[4:8] != b"moov":
        return None
    for off, typ, size, hdr in _atoms(moov, 8):
        if typ != b"mvhd" or size is None:
            continue
        body = off + hdr
        if body + 4 > len(moov):
            return None
        version = moov[body]
        try:
            if version == 1:
                timescale = struct.unpack(">I", moov[body + 20:body + 24])[0]
                duration = struct.unpack(">Q", moov[body + 24:body + 32])[0]
            else:
                timescale = struct.unpack(">I", moov[body + 12:body + 16])[0]
                duration = struct.unpack(">I", moov[body + 16:body + 20])[0]
        except struct.error:
            return None
        if timescale <= 0:
            return None
        return duration / timescale
    return None


def mp4_plan(head: bytes, base: int = 0) -> tuple[str, Any]:
    """Decide the next step from a buffer that starts at file offset `base`:

    ("done", seconds)            — moov was inside the buffer
    ("fetch", (offset, length))  — moov starts at offset; read length bytes
    ("header", offset)           — read a fresh atom header at offset
    ("fail", reason)
    """
    for off, typ, size, hdr in _atoms(head):
        if typ == b"moov":
            if size is None:
                return "fail", "moov of unknown size"
            if size > MOOV_CAP:
                return "fail", f"moov too large ({size} bytes)"
            if off + size <= len(head):
                d = mp4_duration_from_moov(head[off:off + size])
                return ("done", d) if d else ("fail", "no mvhd")
            return "fetch", (base + off, size)
        if size is None:
            return "fail", f"{typ!r} extends to end of file; no moov before it"
        nxt = off + size
        if nxt + 8 > len(head):
            return "header", base + nxt
    return "fail", "no atoms parsed"


# ---------------------------------------------------------------------------
# webm / mkv (EBML)
# ---------------------------------------------------------------------------

_EBML_HEADER = 0x1A45DFA3
_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TIMECODE_SCALE = 0x2AD7B1
_DURATION = 0x4489
_CLUSTER = 0x1F43B675


def _vint(buf: bytes, off: int, keep_marker: bool) -> tuple[int | None, int]:
    """EBML variable-length integer at off → (value, length). value None
    when the size is "unknown" (all value bits set) or the buffer ends."""
    if off >= len(buf):
        return None, 0
    first = buf[off]
    length = 1
    mask = 0x80
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(buf):
        return None, 0
    raw = buf[off:off + length]
    if keep_marker:
        return int.from_bytes(raw, "big"), length
    value = (first & (mask - 1))
    for b in raw[1:]:
        value = (value << 8) | b
    if value == (1 << (7 * length)) - 1:
        return None, length   # unknown size
    return value, length


def webm_duration(head: bytes) -> float | None:
    off = 0
    eid, n = _vint(head, off, True)
    if eid != _EBML_HEADER:
        return None
    off += n
    size, n = _vint(head, off, False)
    if size is None:
        return None
    off += n + size
    eid, n = _vint(head, off, True)
    if eid != _SEGMENT:
        return None
    off += n
    _seg_size, n = _vint(head, off, False)
    off += n
    while off < len(head):
        eid, n = _vint(head, off, True)
        if eid is None:
            return None
        off += n
        size, n = _vint(head, off, False)
        off += n
        if eid == _INFO:
            if size is None:
                return None
            end = min(off + size, len(head))
            scale = 1_000_000
            duration = None
            while off < end:
                cid, n = _vint(head, off, True)
                if cid is None:
                    break
                off += n
                csize, n = _vint(head, off, False)
                off += n
                if csize is None or off + csize > len(head):
                    break
                data = head[off:off + csize]
                if cid == _TIMECODE_SCALE:
                    scale = int.from_bytes(data, "big") or scale
                elif cid == _DURATION:
                    if csize == 4:
                        duration = struct.unpack(">f", data)[0]
                    elif csize == 8:
                        duration = struct.unpack(">d", data)[0]
                off += csize
            return duration * scale / 1e9 if duration else None
        if eid == _CLUSTER or size is None:
            return None
        off += size
    return None


# ---------------------------------------------------------------------------
# network driver
# ---------------------------------------------------------------------------

def looks_like_video(name_or_url: str) -> bool:
    s = (name_or_url or "").lower().split("?", 1)[0]
    return s.endswith(VIDEO_EXTS)


async def _ranged(session: aiohttp.ClientSession, url: str, headers: dict, start: int,
                  length: int) -> bytes:
    end = start + length - 1
    async with session.get(
        url, headers={**headers, "Range": f"bytes={start}-{end}"},
        allow_redirects=True, timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status}")
        if resp.status == 200 and start > 0:
            raise RuntimeError("server ignored Range")
        if resp.status == 206:
            return await resp.read()          # exactly the range
        # A 200 for start=0 hands us the whole file: read just `length`
        # bytes, waiting for them (StreamReader.read(n) would return only
        # what happens to be buffered).
        try:
            return await resp.content.readexactly(length)
        except asyncio.IncompleteReadError as e:
            return e.partial


async def probe_media_duration(
    url: str, session: aiohttp.ClientSession, headers: dict | None = None,
    filename: str = "",
) -> float | None:
    """Seconds, or None. At most a handful of small ranged GETs; never
    raises — a missing duration is not a probe failure."""
    headers = dict(headers or {})
    kind_src = filename or url
    try:
        head = await _ranged(session, url, headers, 0, HEAD_BYTES)
        if not head:
            return None
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return webm_duration(head)
        if len(head) >= 8 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
            step, arg = mp4_plan(head, 0)
            hops = 0
            while hops < MAX_HOPS:
                hops += 1
                if step == "done":
                    return arg
                if step == "fail":
                    logger.debug("mp4 duration: %s (%s)", arg, url[:80])
                    return None
                if step == "fetch":
                    off, size = arg
                    moov = await _ranged(session, url, headers, off, size)
                    d = mp4_duration_from_moov(moov)
                    return d
                if step == "header":
                    off = arg
                    hdr = await _ranged(session, url, headers, off, 16)
                    step, arg = mp4_plan(hdr, off)
                    # mp4_plan on a bare 16-byte header: either "fetch" (moov
                    # size known) or "header" for the following atom.
                    continue
            return None
        if looks_like_video(kind_src):
            logger.debug("duration: unrecognised container for %s", url[:80])
        return None
    except Exception as e:
        logger.debug("duration probe failed for %s: %s", url[:80], e)
        return None
