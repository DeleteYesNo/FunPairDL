"""Format-level http_headers must reach ResolvedFile.headers.

When the direct URL comes from a picked formats[] entry, yt-dlp stores the
negotiated headers on that entry, not at the top level of the info dict.
VK's CDN (vkuser.net) rejects header-less requests with HTTP 400, so
dropping them turns every segment request into a permanent failure.
"""
import asyncio
import sys
import types

import pytest


FAKE_INFO = {
    "title": "vid",
    "formats": [{
        "format_id": "url1080",
        "url": "https://vk6-1.vkuser.net/x.mp4",
        "ext": "mp4",
        "height": 1080,
        "filesize": 1000,
        "http_headers": {"User-Agent": "UA-NEGOTIATED"},
    }],
    # deliberately NO top-level "url" and NO top-level "http_headers"
}


class _FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(FAKE_INFO)


def test_selected_format_headers_propagate(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider

    resolved = asyncio.run(YtdlpGenericProvider().resolve("https://vk.com/video-1_2"))
    assert resolved.direct_url == "https://vk6-1.vkuser.net/x.mp4"
    assert resolved.headers == {"User-Agent": "UA-NEGOTIATED"}


class _RecordingYDL:
    """Records the opts each YoutubeDL was built with."""
    seen_opts: list = []

    def __init__(self, opts):
        _RecordingYDL.seen_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(FAKE_INFO)


def _install_fake_ytdlp_with_impersonation(monkeypatch):
    class _Target:
        def __init__(self, client=""):
            self.client = client

    imp_mod = types.SimpleNamespace(ImpersonateTarget=_Target)
    net_mod = types.SimpleNamespace(impersonate=imp_mod)
    yd_mod = types.SimpleNamespace(YoutubeDL=_RecordingYDL, networking=net_mod)
    monkeypatch.setitem(sys.modules, "yt_dlp", yd_mod)
    monkeypatch.setitem(sys.modules, "yt_dlp.networking", net_mod)
    monkeypatch.setitem(sys.modules, "yt_dlp.networking.impersonate", imp_mod)


def test_vk_skips_impersonation(monkeypatch):
    # VK signs CDN URLs against the extraction client's fingerprint; the
    # aiohttp downloader can't replay curl_cffi's, so impersonated extraction
    # produces URLs that 400 forever. VK must extract with plain headers.
    _install_fake_ytdlp_with_impersonation(monkeypatch)
    from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider

    _RecordingYDL.seen_opts = []
    asyncio.run(YtdlpGenericProvider().resolve("https://vk.com/video-1_2"))
    assert all("impersonate" not in o for o in _RecordingYDL.seen_opts)


def test_other_hosts_still_impersonate(monkeypatch):
    _install_fake_ytdlp_with_impersonation(monkeypatch)
    from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider

    _RecordingYDL.seen_opts = []
    asyncio.run(YtdlpGenericProvider().resolve("https://rule34video.com/video/123"))
    assert any("impersonate" in o for o in _RecordingYDL.seen_opts)
