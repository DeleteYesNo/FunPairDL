"""Regression tests for SegmentDownloader error handling."""
import asyncio
import ssl

import pytest

from funpairdl.core.segment import SegmentDownloader


class _BoomContext:
    """Async context manager that fails on enter — mimics aiohttp raising a
    connection/SSL error from `async with session.get(...)`."""

    async def __aenter__(self):
        raise RuntimeError("connection refused")

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def get(self, *args, **kwargs):
        return _BoomContext()


def test_connection_failure_surfaces_real_error(tmp_path):
    # When the connection itself fails before the streaming loop assigns `buf`,
    # the except/flush path must not raise an UnboundLocalError that masks the
    # real cause. (A non-TLS error must also propagate, not get retried.)
    seg = SegmentDownloader(
        url="https://broken.example/seg",
        range_start=0, range_end=100,
        temp_file=tmp_path / "seg.part", index=0,
    )
    with pytest.raises(RuntimeError, match="connection refused"):
        asyncio.run(seg.download(_FakeSession()))


class _Resp:
    def __init__(self, data):
        self._data = data
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def content(self):
        data = self._data

        class _Content:
            async def iter_chunked(self, _n):
                yield data

        return _Content()


class _CertFailThenOK:
    """Rejects the cert when verification is on (ssl=True), serves the bytes
    when verification is off (ssl=False) — mimics a host with an expired cert."""

    def __init__(self):
        self.ssl_calls = []

    def get(self, url, headers=None, ssl=None, timeout=None):
        self.ssl_calls.append(ssl)
        if ssl is not False:
            class _Fail:
                async def __aenter__(self_):
                    raise __import__("ssl").SSLCertVerificationError("certificate has expired")
                async def __aexit__(self_, *e):
                    return False
            return _Fail()
        return _Resp(b"video-bytes")


def test_retries_without_verification_on_cert_error(tmp_path):
    # A rejected TLS cert must fall back to an unverified retry for THAT host,
    # and the download then succeeds.
    out = tmp_path / "seg.part"
    seg = SegmentDownloader(
        url="https://expired-cert-cdn.example/seg",
        range_start=0, range_end=100,
        temp_file=out, index=0, use_range=False,
    )
    sess = _CertFailThenOK()
    asyncio.run(seg.download(sess))
    assert out.read_bytes() == b"video-bytes"
    assert sess.ssl_calls == [True, False]  # tried verified first, then not


# --- audit [14]: no-range downloads must not resume/complete from partials ---


class _OKSession:
    def __init__(self, data):
        self._data = data
        self.calls = 0

    def get(self, url, headers=None, ssl=None, timeout=None):
        self.calls += 1
        return _Resp(self._data)


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        raise AssertionError("network must not be touched")


class _MidStreamFail:
    """Yields one chunk, then dies — mimics a connection reset mid-stream."""

    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def content(self):
        class _Content:
            async def iter_chunked(self, _n):
                yield b"OLD-PARTIAL-"
                raise RuntimeError("connection reset mid-stream")

        return _Content()


class _FlakyThenOK:
    def __init__(self, data):
        self._data = data
        self.calls = 0

    def get(self, url, headers=None, ssl=None, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return _MidStreamFail()
        return _Resp(self._data)


def test_norange_stale_partial_is_discarded_and_redownloaded(tmp_path):
    # A leftover .part0 from a previous run/pause used to satisfy the bogus
    # 1-byte is_complete expectation and get stamped complete instantly.
    # It must instead be deleted and the whole file re-downloaded.
    out = tmp_path / "file.part0"
    out.write_bytes(b"stale-partial")
    seg = SegmentDownloader(
        url="https://norange.example/f",
        range_start=0, range_end=0,
        temp_file=out, index=0, use_range=False,
    )
    sess = _OKSession(b"the-complete-file-bytes")
    asyncio.run(seg.download(sess))
    assert sess.calls == 1  # actually re-downloaded, no instant "success"
    assert out.read_bytes() == b"the-complete-file-bytes"  # replaced, not appended


def test_norange_is_complete_requires_known_total(tmp_path):
    # is_complete must never derive a fake 1-byte expectation from the
    # placeholder range for no-range plans.
    seg = SegmentDownloader(
        url="u", range_start=0, range_end=0,
        temp_file=tmp_path / "f.part0", index=0, use_range=False,
    )
    seg.downloaded = 5
    assert not seg.is_complete  # unknown total → never complete
    seg.known_total = 5
    assert seg.is_complete  # caller-supplied real total → provable


def test_norange_existing_file_matching_known_total_is_kept(tmp_path):
    # With a real total from the caller, a fully-downloaded temp file is
    # recognized without touching the network.
    out = tmp_path / "file.part0"
    out.write_bytes(b"0123456789")
    seg = SegmentDownloader(
        url="https://norange.example/f",
        range_start=0, range_end=0,
        temp_file=out, index=0, use_range=False, known_total=10,
    )
    asyncio.run(seg.download(_NoNetworkSession()))
    assert out.read_bytes() == b"0123456789"
    assert seg.downloaded == 10


def test_norange_midstream_retry_restarts_from_scratch(tmp_path):
    # Round-based retry re-calls download() on the same instance after a
    # mid-stream error flushed a partial. The retry must restart the whole
    # file (delete partial, byte 0), not append to it or declare it done.
    out = tmp_path / "file.part0"
    seg = SegmentDownloader(
        url="https://norange.example/f",
        range_start=0, range_end=0,
        temp_file=out, index=0, use_range=False,
    )
    sess = _FlakyThenOK(b"fresh-complete-content")

    with pytest.raises(RuntimeError, match="mid-stream"):
        asyncio.run(seg.download(sess))
    assert out.read_bytes() == b"OLD-PARTIAL-"  # best-effort flush happened

    asyncio.run(seg.download(sess))  # the retry round
    assert sess.calls == 2
    assert out.read_bytes() == b"fresh-complete-content"
    assert seg.downloaded == len(b"fresh-complete-content")


# --- permanent 4xx errors carry the host's JSON diagnostic when present ---


class _ErrResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def content(self):
        body = self._body
        state = {"pos": 0}

        class _C:
            async def read(self, n=-1):
                start = state["pos"]
                if start >= len(body):
                    return b""  # EOF, like aiohttp's StreamReader
                end = len(body) if n is None or n < 0 else start + n
                state["pos"] = min(end, len(body))
                return body[start:state["pos"]]

        return _C()


class _ErrSession:
    def __init__(self, status, body):
        self._status = status
        self._body = body

    def get(self, url, headers=None, ssl=None, timeout=None):
        return _ErrResp(self._status, self._body)


def test_permanent_4xx_includes_json_error_detail(tmp_path):
    # Pixeldrain-style 403 bodies explain WHY (hotlink captcha); the reason
    # must land in the error message instead of an opaque status code.
    body = (b'{"success":false,"value":"file_rate_limited_captcha_required",'
            b'"message":"We have detected the use of hotlinking for this file."}')
    seg = SegmentDownloader(
        url="https://pixeldrain.com/api/file/xyz",
        range_start=0, range_end=100,
        temp_file=tmp_path / "seg.part", index=0,
    )
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(seg.download(_ErrSession(403, body)))
    msg = str(ei.value)
    assert "(permanent)" in msg  # download_task's abort marker must survive
    assert "file_rate_limited_captcha_required" in msg
    assert "hotlinking" in msg


def test_permanent_4xx_with_unreadable_body_keeps_plain_message(tmp_path):
    seg = SegmentDownloader(
        url="https://cdn.example/seg",
        range_start=0, range_end=100,
        temp_file=tmp_path / "seg.part", index=0,
    )
    with pytest.raises(RuntimeError, match=r"HTTP 404 \(permanent\) for segment 0$"):
        asyncio.run(seg.download(_ErrSession(404, b"<html>not json</html>")))


def test_permanent_4xx_detail_survives_fragmented_reads(tmp_path):
    # aiohttp's read(n) may return fewer bytes than asked; the JSON detail
    # must still be assembled from split deliveries.
    body = b'{"value":"captcha","message":"solve it"}'

    class _TrickleResp(_ErrResp):
        @property
        def content(self):
            data = self._body
            state = {"pos": 0}

            class _C:
                async def read(self, n=-1):
                    if state["pos"] >= len(data):
                        return b""
                    chunk = data[state["pos"]:state["pos"] + 3]  # 3B fragments
                    state["pos"] += len(chunk)
                    return chunk

            return _C()

    class _TrickleSession:
        def get(self, url, headers=None, ssl=None, timeout=None):
            return _TrickleResp(403, body)

    seg = SegmentDownloader(
        url="https://cdn.example/seg",
        range_start=0, range_end=100,
        temp_file=tmp_path / "seg.part", index=0,
    )
    with pytest.raises(RuntimeError, match="captcha — solve it"):
        asyncio.run(seg.download(_TrickleSession()))
