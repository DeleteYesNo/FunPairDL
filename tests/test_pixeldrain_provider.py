"""Resolve-time availability detection for Pixeldrain files.

Pixeldrain's /api/file/{id}/info announces per-file download blocks in an
"availability" field (e.g. file_rate_limited_captcha_required — the hotlink
limit that only a paid account or a browser captcha lifts). Resolve must
surface that as an actionable error instead of letting the download die with
an opaque "HTTP 403 (permanent) for segment 0".
"""
import asyncio

import pytest

from funpairdl.providers.pixeldrain import PixeldrainProvider


def _patch_info(monkeypatch, payload):
    async def fake_get_json(url, headers):
        assert url.endswith("/info")
        return payload

    monkeypatch.setattr(PixeldrainProvider, "_get_json", staticmethod(fake_get_json))


def test_resolve_surfaces_rate_limit_at_resolve_time(monkeypatch):
    _patch_info(monkeypatch, {
        "name": "clip.funscript",
        "size": 18496,
        "availability": "file_rate_limited_captcha_required",
        "availability_message": "We have detected the use of hotlinking for this file.",
    })
    provider = PixeldrainProvider(api_key="k")
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(provider.resolve("https://pixeldrain.com/api/file/CcCc0003"))
    msg = str(ei.value)
    assert "file_rate_limited_captcha_required" in msg
    assert "hotlinking" in msg
    assert "pixeldrain.com/u/CcCc0003" in msg  # actionable manual fallback


def test_resolve_ok_when_no_availability_flag(monkeypatch):
    _patch_info(monkeypatch, {"name": "clip.mp4", "size": 42, "availability": ""})
    provider = PixeldrainProvider(api_key="k")
    resolved = asyncio.run(provider.resolve("https://pixeldrain.com/api/file/abc123"))
    assert resolved.direct_url.endswith("/api/file/abc123")
    assert resolved.filename == "clip.mp4"
    assert resolved.total_size == 42
    assert "Authorization" in resolved.headers
