"""e621 post parsing and routing.

The network side is one JSON call, so these cover the pure pieces that
decide *which* rendition the downloader is handed and what it is called.
Fixture data mirrors the real API shape with synthetic ids and hashes.
"""
import pytest

from funpairdl.providers.e621 import (
    E621Provider,
    api_url,
    build_filename,
    build_formats,
    describe_unavailable,
    extract_post_id,
    select_format,
    title_from_html,
    title_from_tags,
)
from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
from funpairdl.utils.url_parser import detect_provider

CDN = "https://static1.e621.net/data"
POST = {
    "id": 1234567,
    "file": {"width": 600, "height": 800, "ext": "webm", "size": 97_000_000,
             "md5": "0" * 32, "url": f"{CDN}/00/00/{'0' * 32}.webm"},
    "sample": {"alternates": {
        "has": True,
        "original": {"codec": "vp9", "size": 97_000_000, "width": 600, "height": 800,
                     "url": f"{CDN}/00/00/{'0' * 32}.webm"},
        "variants": {"mp4": {"codec": "avc1", "size": 19_000_000, "width": 600,
                             "height": 800, "url": f"{CDN}/sample/00/00/{'0' * 32}_alt.mp4"}},
        "samples": {"480p": {"codec": "avc1", "size": 13_000_000, "width": 480,
                             "height": 640, "url": f"{CDN}/sample/00/00/{'0' * 32}_480p.mp4"}},
    }},
    "tags": {"artist": ["sound_warning", "someartist"], "character": ["oc"]},
    "flags": {"deleted": False},
}


@pytest.mark.parametrize("url,expected", [
    ("https://e621.net/posts/1234567", 1234567),
    ("https://e621.net/posts/1234567?q=someartist", 1234567),
    ("https://www.e926.net/posts/42/", 42),
    ("https://e621.net/post/show/99", 99),
    ("https://e621.net/posts?tags=foo", None),
    ("https://e621.net/pools/12", None),
    ("https://example.com/posts/1", None),
    ("not a url", None),
])
def test_extract_post_id(url, expected):
    assert extract_post_id(url) == expected
    assert E621Provider.can_handle(url) is (expected is not None)


def test_api_url_keeps_host_and_drops_query():
    assert api_url("https://e621.net/posts/1234567?q=x") == "https://e621.net/posts/1234567.json"
    assert api_url("https://e926.net/posts/5") == "https://e926.net/posts/5.json"


def test_routing_prefers_e621_over_ytdlp_generic():
    url = "https://e621.net/posts/1234567?q=someartist"
    assert detect_provider(url) == "e621"
    assert detect_provider("https://e926.net/posts/1") == "e621"
    assert YtdlpGenericProvider.can_handle(url) is False


def test_build_formats_ascending_with_original_last():
    fmts = build_formats(POST)
    assert [f["label"] for f in fmts] == ["480p", "mp4", "original"]
    assert [f["height"] for f in fmts] == [640, 800, 800]
    assert fmts[-1]["original"] is True and fmts[-1]["ext"] == "webm"
    assert fmts[0]["ext"] == "mp4"


def test_select_format_best_is_original_and_exact_height_prefers_mp4():
    fmts = build_formats(POST)
    assert select_format(fmts, "best")["label"] == "original"
    assert select_format(fmts, "")["label"] == "original"
    assert select_format(fmts, "640")["label"] == "480p"
    # 800 ties between the mp4 transcode and the webm original: mp4 wins
    # when the user asked for a height rather than "best".
    assert select_format(fmts, "800")["label"] == "mp4"
    assert select_format(fmts, "1080")["label"] == "original"
    assert select_format(fmts, "junk")["label"] == "original"
    assert select_format([], "best") is None


def test_hidden_post_has_no_formats():
    hidden = {**POST, "file": {**POST["file"], "url": None},
              "sample": {"alternates": {}}}
    assert build_formats(hidden) == []
    assert "login" in describe_unavailable(hidden)
    assert "deleted" in describe_unavailable({**hidden, "flags": {"deleted": True}})


def test_filename_prefers_the_page_title_then_tags_then_id():
    fmts = build_formats(POST)
    # The page's og:title, as the forum's link card shows it, wins.
    assert build_filename(POST, fmts[-1], "oc (somegame and etc) created by someartist") \
        == "oc (somegame and etc) created by someartist.webm"
    # Offline: built from tags in e621's own wording.
    assert build_filename(POST, fmts[0]) == "oc created by someartist.mp4"
    bare = {**POST, "tags": {"artist": ["sound_warning"]}}
    assert build_filename(bare, fmts[-1]) == "e621_1234567.webm"


def test_title_from_html_reads_og_title_and_drops_site_suffix():
    html = ('<meta name="description" content="x">'
            '<meta property="og:title" content="oc (somegame and etc) created by someartist - e621">')
    assert title_from_html(html) == "oc (somegame and etc) created by someartist"
    assert title_from_html('<meta property="og:title" content="#1234567 - e621">') == ""
    assert title_from_html("<html></html>") == ""


def test_title_from_tags_mirrors_e621_wording():
    post = {"tags": {
        "character": ["alpha_(somegame)", "beta"],
        "copyright": ["somegame", "somecompany"],
        "artist": ["someartist", "sound_warning", "conditional_dnp"],
    }}
    assert title_from_tags(post) == "alpha and beta (somegame and etc) created by someartist"
    assert title_from_tags({"tags": {"copyright": ["somegame"], "artist": ["a_b"]}}) \
        == "(somegame) created by a b"
    assert title_from_tags({"tags": {}}) == ""
