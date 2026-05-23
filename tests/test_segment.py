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
