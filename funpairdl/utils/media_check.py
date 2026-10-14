"""Cheap structural check of a finished download.

An MP4/MOV is a chain of top-level boxes whose sizes must add up to exactly
the file size, with a ``moov`` (the index) somewhere in it. A download that
stopped early leaves the last box (usually ``mdat``) claiming more bytes than
the file has; one stitched from wrong parts leaves the chain running into
garbage. Either way the file plays only partly, if at all — and before this
check such files were marked COMPLETED. Other containers are not checked.
"""
from __future__ import annotations

import struct
from pathlib import Path

MP4_SUFFIXES = {".mp4", ".m4v", ".mov"}


def mp4_problem(path: Path) -> str | None:
    """A short reason when ``path`` is an MP4/MOV whose box chain does not
    fit the file, else None (also for other containers and empty names)."""
    if path.suffix.lower() not in MP4_SUFFIXES:
        return None
    size = path.stat().st_size
    if size == 0:
        return "empty file"
    off = 0
    seen = set()
    with open(path, "rb") as f:
        while off < size:
            f.seek(off)
            head = f.read(16)
            if len(head) < 8:
                if off:
                    # Under 8 bytes after a whole chain can hold no box:
                    # padding some uploaders leave (three spaces), not a cut.
                    break
                return f"box header cut off at byte {off}"
            box_size, box_type = struct.unpack(">I4s", head[:8])
            if box_size == 1:
                if len(head) < 16:
                    return f"box header cut off at byte {off}"
                box_size = struct.unpack(">Q", head[8:16])[0]
            elif box_size == 0:
                box_size = size - off  # runs to the end of the file
            if box_size < 8:
                return f"invalid box at byte {off}"
            seen.add(box_type)
            off += box_size
    if off > size:
        return f"truncated: {off - size} bytes missing"
    if b"moov" not in seen:
        return "no moov (index) box"
    return None
