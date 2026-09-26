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


def test_funscript_info_reads_combined_multi_axis_files():
    import json
    from funpairdl.utils.media_duration import funscript_info
    doc = {"version": "1.1", "actions": [], "axes": [
        {"id": "R0", "actions": [{"at": 0, "pos": 50}, {"at": 90_000, "pos": 60}]},
        {"id": "R1", "actions": [{"at": 0, "pos": 50}, {"at": 120_000, "pos": 40}]}]}
    assert funscript_info(json.dumps(doc).encode()) ["duration"] == 120.0


def _tkhd(width: int, height: int) -> bytes:
    body = bytes(4) + bytes(20) + bytes(8) + bytes(8) + bytes(36)
    body += struct.pack(">II", width << 16, height << 16)
    return _atom(b"tkhd", body)


def _mp4(seconds: float, width: int, height: int) -> bytes:
    moov = _atom(b"moov", _mvhd(1000, int(seconds * 1000))
                 + _atom(b"trak", _tkhd(0, 0))                 # audio: 0x0
                 + _atom(b"trak", _tkhd(width, height)))
    return _atom(b"ftyp", b"isom" + bytes(12)) + moov + _atom(b"mdat", bytes(64))


def test_frame_size_from_the_video_track(tmp_path):
    from funpairdl.utils.media_duration import local_media_meta, mp4_dims_from_moov
    data = _mp4(160.0, 7680, 3840)
    moov_at = data.index(b"moov") - 4
    assert mp4_dims_from_moov(data[moov_at:moov_at + int.from_bytes(data[moov_at:moov_at + 4], "big")]) == (7680, 3840)
    p = tmp_path / "v.mp4"
    p.write_bytes(data)
    assert local_media_meta(p) == {"duration": 160.0, "width": 7680, "height": 3840}


def test_mega_media_attribute_round_trip():
    import base64
    from funpairdl.utils.mega_api import media_attributes

    def encrypt(v, k):
        n, delta, mask = len(v), 0x9E3779B9, 0xFFFFFFFF
        rounds, s, z = 6 + 52 // n, 0, v[-1]
        for _ in range(rounds):
            s = (s + delta) & mask
            e = (s >> 2) & 3
            for p in range(n):
                y = v[(p + 1) % n]
                mx = ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & mask
                v[p] = (v[p] + mx) & mask
                z = v[p]
        return v

    width, height, fps, secs = 3840, 2160, 60, 160
    b = bytes([(width << 1) & 255, (width >> 7) & 127, height & 255, ((height >> 8) & 127) | ((fps & 1) << 7),
               ((fps >> 1) & 127) | ((secs & 1) << 7), (secs >> 1) & 255, (secs >> 9) & 255, 16])
    key = (1, 2, 3, 4, 0x11111111, 0x22222222, 0x33333333, 0x44444444)
    enc = struct.pack("<2I", *encrypt(list(struct.unpack("<2I", b)), list(key[4:8])))
    fa = "123:0*abc/456:8*" + base64.urlsafe_b64encode(enc).decode().rstrip("=")
    assert media_attributes(fa, key) == {"width": 3840, "height": 2160, "fps": 60, "duration": 160}
    assert media_attributes("123:0*abc", key) == {}
