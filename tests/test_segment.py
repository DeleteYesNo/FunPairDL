"""Regression tests for SegmentDownloader error handling."""
import asyncio

import pytest

from funpairdl.core.segment import SegmentDownloader


class _BoomContext:
    """Async context manager that fails on enter — mimics aiohttp raising a
    connection/SSL error from `async with session.get(...)`."""

    async def __aenter__(self):
        raise RuntimeError("SSL: certificate has expired")

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def get(self, *args, **kwargs):
        return _BoomContext()


def test_connection_failure_surfaces_real_error(tmp_path):
    # When the connection itself fails (e.g. an expired-cert SSL error) before
    # the streaming loop assigns `buf`, the except/flush path must not raise an
    # UnboundLocalError that masks the real cause.
    seg = SegmentDownloader(
        url="https://expired-cert.example/seg",
        range_start=0,
        range_end=100,
        temp_file=tmp_path / "seg.part",
        index=0,
    )
    with pytest.raises(RuntimeError, match="certificate has expired"):
        asyncio.run(seg.download(_FakeSession()))
