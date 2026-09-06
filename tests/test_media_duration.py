"""Duration parsers: funscript, mp4 (faststart and moov-after-mdat), webm."""
import json
import struct

from funpairdl.utils.media_duration import (
    funscript_info, mp4_duration_from_moov, mp4_plan, webm_duration,
)


def _atom(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + typ + body


def _mvhd(timescale: int, duration: int) -> bytes:
    body = bytes([0, 0, 0, 0]) + struct.pack(">II", 0, 0) + struct.pack(">II", timescale, duration)
    body += b"\x00" * 80
    return _atom(b"mvhd", body)


def _moov(timescale=1000, duration=201_500) -> bytes:
    return _atom(b"moov", _mvhd(timescale, duration) + _atom(b"trak", b"\x00" * 16))


def test_funscript_duration_from_last_action_and_metadata():
    doc = {"actions": [{"at": 0, "pos": 50}, {"at": 12_340, "pos": 90}, {"at": 201_400, "pos": 10}],
           "metadata": {"duration": 201.9, "title": "Alpha Scene", "video_url": "https://host.example/a.mp4"}}
    info = funscript_info(json.dumps(doc).encode())
    assert info["duration"] == 201.9           # metadata agrees with actions → trusted
    assert info["title"] == "Alpha Scene"
    assert info["video_url"] == "https://host.example/a.mp4"
    # Junk metadata (far off the action span) is ignored.
    doc["metadata"]["duration"] = 5
    assert funscript_info(json.dumps(doc).encode())["duration"] == 201.4
    assert funscript_info(b"not json")["duration"] is None


def test_mp4_faststart_duration_from_head():
    head = _atom(b"ftyp", b"isom" + b"\x00" * 12) + _moov() + _atom(b"mdat", b"\x00" * 64)
    assert mp4_plan(head) == ("done", 201.5)


def test_mp4_moov_after_mdat_needs_two_hops():
    ftyp = _atom(b"ftyp", b"isom" + b"\x00" * 12)
    mdat = struct.pack(">I", 8 + 5_000_000) + b"mdat"   # header only; body not in buffer
    head = ftyp + mdat + b"\x00" * 100
    step, off = mp4_plan(head)
    assert step == "header" and off == len(ftyp) + 8 + 5_000_000
    # Reading the atom header at that offset reveals moov and its size.
    moov = _moov(timescale=90_000, duration=90_000 * 61)
    step, (moff, msize) = mp4_plan(moov[:16], off)
    assert step == "fetch" and moff == off and msize == len(moov)
    assert mp4_duration_from_moov(moov) == 61.0


def test_mp4_64bit_mdat_size():
    ftyp = _atom(b"ftyp", b"isom" + b"\x00" * 12)
    mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + 7_000_000_000)
    step, off = mp4_plan(ftyp + mdat + b"\x00" * 32)
    assert step == "header" and off == len(ftyp) + 16 + 7_000_000_000


def _ebml(eid: int, payload: bytes) -> bytes:
    idb = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    n = len(payload)
    # 2-byte size vint (marker 0x40) is enough for test payloads (< 16383).
    return idb + bytes([0x40 | (n >> 8), n & 0xFF]) + payload


def test_webm_duration_from_segment_info():
    info = _ebml(0x2AD7B1, (1_000_000).to_bytes(3, "big")) + _ebml(0x4489, struct.pack(">d", 154_320.0))
    segment_children = _ebml(0x114D9B74, b"\x00" * 4) + _ebml(0x1549A966, info) + _ebml(0x1654AE6B, b"\x00" * 4)
    head = _ebml(0x1A45DFA3, _ebml(0x4282, b"webm")) + _ebml(0x18538067, segment_children)
    assert abs(webm_duration(head) - 154.32) < 1e-6
    assert webm_duration(b"\x00" * 32) is None
