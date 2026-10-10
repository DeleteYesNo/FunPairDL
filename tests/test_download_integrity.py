"""A download is COMPLETED only when the file on disk is the file served.

Regressions for files that were marked done while broken: a leftover part
bigger than its segment merged as-is, a stream that ended cleanly but short,
a server that ignores Range, and the structural check that catches what
slips through (truncated MP4s) before a pair is filed."""
import asyncio
import os
import struct
from types import SimpleNamespace

import pytest
from aiohttp import web

from funpairdl.core import download_task as dt
from funpairdl.core.download_task import DownloadTask
from funpairdl.core.pair import FileType, ItemState, PairItem
from funpairdl.core.queue_manager import QueueManager
from funpairdl.utils.media_check import mp4_problem

DATA = os.urandom(24 * 1024 * 1024)  # 24 MB: above SMALL_FILE_THRESHOLD, so multi-segment


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(dt, "SEGMENT_RETRY_DELAYS", (0,))


def _serve(mode):
    """mode: 'ok' | 'short-once' (each range's first answer is half, no
    error) | 'ignore-range' (always 200 with the whole file)."""
    seen = set()

    async def handler(req):
        if req.method == "HEAD":
            return web.Response(headers={"Content-Length": str(len(DATA)), "Accept-Ranges": "bytes"})
        rng = req.headers.get("Range")
        if not rng or mode == "ignore-range":
            return web.Response(body=DATA)
        a, b = rng.split("=")[1].split("-")
        a, b = int(a), (int(b) if b else len(DATA) - 1)
        if mode == "short-once" and b not in seen:
            seen.add(b)
            b = a + (b - a) // 2
        return web.Response(status=206, body=DATA[a:b + 1],
                            headers={"Content-Range": f"bytes {a}-{b}/{len(DATA)}", "Accept-Ranges": "bytes"})
    return handler


async def _download(tmp_path, mode, prep=None):
    app = web.Application()
    app.router.add_route("*", "/{name}", _serve(mode))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    item = PairItem(url=f"http://127.0.0.1:{port}/v.mp4", filename="v.mp4", file_type=FileType.VIDEO)
    task = DownloadTask(item=item, output_dir=tmp_path, num_segments=8)
    if prep:
        prep(task)
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            try:
                await task.download(s)
            except Exception:
                pass
    finally:
        await runner.cleanup()
    out = tmp_path / "v.mp4"
    return item, (out.read_bytes() if out.exists() else None)


def test_clean_download(tmp_path):
    item, data = asyncio.run(_download(tmp_path, "ok"))
    assert item.state == ItemState.COMPLETED and data == DATA
    assert item.downloaded_bytes == len(DATA)


def test_oversized_leftover_parts_are_not_merged(tmp_path):
    def stale(task):
        task.parts_dir.mkdir(parents=True, exist_ok=True)
        # the old `<name>.partN` naming, from an attempt that split differently
        (task.parts_dir / "v.mp4.part1").write_bytes(b"\0" * (8 * 1024 * 1024))
        # same range name, but more bytes than the range (doubled appends)
        seg = 3 * 1024 * 1024
        (task.parts_dir / f"v.mp4.r{seg}-{2 * seg - 1}.part").write_bytes(b"\1" * (seg + 4096))

    item, data = asyncio.run(_download(tmp_path, "ok", stale))
    assert item.state == ItemState.COMPLETED and data == DATA
    assert not any((tmp_path / ".parts").glob("v.mp4.*")), "leftovers are cleaned up"


def test_clean_but_short_stream_is_resumed(tmp_path):
    item, data = asyncio.run(_download(tmp_path, "short-once"))
    assert item.state == ItemState.COMPLETED and data == DATA


def test_server_ignoring_range_fails_instead_of_stitching_garbage(tmp_path):
    item, data = asyncio.run(_download(tmp_path, "ignore-range"))
    assert item.state == ItemState.FAILED
    assert data is None


# ─── MP4 structure check ───

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def test_mp4_problem_accepts_a_whole_file(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", b"m" * 40) + _box(b"mdat", b"d" * 1000))
    assert mp4_problem(p) is None


def test_mp4_problem_flags_truncation_and_missing_index(tmp_path):
    whole = _box(b"ftyp", b"isom") + _box(b"moov", b"m" * 40) + _box(b"mdat", b"d" * 1000)
    p = tmp_path / "a.mp4"
    p.write_bytes(whole[:-300])
    assert "truncated: 300 bytes missing" == mp4_problem(p)
    p.write_bytes(_box(b"ftyp", b"isom") + _box(b"mdat", b"d" * 1000))
    assert "no moov" in mp4_problem(p)
    p.write_bytes(b"")
    assert mp4_problem(p) == "empty file"


def test_mp4_problem_ignores_other_containers(tmp_path):
    p = tmp_path / "a.mkv"
    p.write_bytes(b"\0" * 100)
    assert mp4_problem(p) is None


def test_broken_video_is_failed_and_removed_but_foreign_files_are_left(tmp_path):
    broken = _box(b"ftyp", b"isom") + _box(b"moov", b"m" * 40) + _box(b"mdat", b"d" * 1000)
    (tmp_path / "new.mp4").write_bytes(broken[:-100])
    (tmp_path / "library.mp4").write_bytes(broken[:-100])  # was in the folder before the pair
    new = PairItem(url="u1", filename="new.mp4", file_type=FileType.VIDEO)
    lib = PairItem(url="u2", filename="library.mp4", file_type=FileType.VIDEO)
    new.state = lib.state = ItemState.COMPLETED
    pair = SimpleNamespace(items=[new, lib], foreign_files=["library.mp4"])
    fake = SimpleNamespace(on_item_updated=None)
    asyncio.run(QueueManager._fail_broken_videos(fake, pair, tmp_path))
    assert new.state == ItemState.FAILED and "truncated" in new.error_message
    assert not (tmp_path / "new.mp4").exists()
    assert lib.state == ItemState.COMPLETED and (tmp_path / "library.mp4").exists()
