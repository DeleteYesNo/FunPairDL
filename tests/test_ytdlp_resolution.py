"""A pre-selected top-level info["url"] must not override a requested height.

Some extractors (BunnyCdn, which backs part of socigames.com) resolve their
own best format and put it in info["url"] alongside the formats list. The
resolver used to take that URL whenever it was present, so _select_format
never ran and asking for 480p still downloaded 1080p — silently, since the
download itself succeeded.
"""
import asyncio
import sys
import types

import pytest


def _info_with_top_level_url():
    """Shape yt-dlp returns for BunnyCdn: best format hoisted to info["url"]."""
    fmts = [
        {"format_id": str(h), "url": f"https://cdn.example/{h}p/video.m3u8",
         "ext": "mp4", "height": h, "vcodec": "avc1", "acodec": "mp4a"}
        for h in (360, 480, 720, 1080)
    ]
    return {"title": "clip", "url": "https://cdn.example/1080p/video.m3u8",
            "formats": fmts}


class _FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return _info_with_top_level_url()


def _resolve(monkeypatch, resolution):
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
    return asyncio.run(YtdlpGenericProvider().resolve(
        "https://iframe.mediadelivery.net/embed/1/abc",
        preferred_resolution=resolution,
    ))


@pytest.mark.parametrize("resolution,expected_height", [
    ("480", 480),
    ("720", 720),
    ("360", 360),
])
def test_requested_height_wins_over_pre_selected_url(monkeypatch, resolution, expected_height):
    resolved = _resolve(monkeypatch, resolution)
    assert resolved.direct_url == f"https://cdn.example/{expected_height}p/video.m3u8"
    assert f"[{expected_height}p]" in resolved.filename


@pytest.mark.parametrize("resolution", ["best", "", "not-a-number"])
def test_best_and_junk_leave_the_extractor_choice_untouched(monkeypatch, resolution):
    """The default path must keep using exactly what yt-dlp picked — the fix
    is only allowed to divert when a specific height was asked for."""
    assert _resolve(monkeypatch, resolution).direct_url == "https://cdn.example/1080p/video.m3u8"


def test_unavailable_height_keeps_the_extractor_choice(monkeypatch):
    # 2160p isn't offered; falling back to the pre-selected best is correct,
    # and must not silently pick some other format instead.
    assert _resolve(monkeypatch, "2160").direct_url == "https://cdn.example/1080p/video.m3u8"
