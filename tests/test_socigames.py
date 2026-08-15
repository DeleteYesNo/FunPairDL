"""SociGames page parsing and routing.

The site is only reachable with browser impersonation, so the network side is
not exercised here — these cover the pure parsing that decides *which* URL the
downloader is handed, which is where the site's two traps live:

  * every page carries autoplaying ad banners in <video> tags, and
  * the real player is lazy-loaded, so its source hides in data-src / a JSON
    block attribute rather than a plain src.
"""
import pytest

from funpairdl.providers.socigames import (
    SociGamesProvider,
    parse_bunny_embed,
    parse_page_title,
    parse_video_sources,
    select_by_resolution,
)

PAGE_URL = "https://socigames.com/demo-work-bouquetman/"

# Trimmed from the real page: three self-hosted ad banners around one
# off-site player source, which appears both in the block config and markup.
DIRECT_PAGE = """
<title>Demo Work [Bouquetman] &raquo; SOCIGAMES</title>
<video autoplay loop muted playsinline style="width:730px;">
  <source src="https://socigames.com/wp-content/uploads/2026/02/banner_a.mp4" type="video/mp4">
</video>
<div block-attributes='{&quot;preload&quot;:&quot;metadata&quot;,&quot;id&quot;:3398,
 &quot;src&quot;:&quot;https:\\/\\/fappingstream.com\\/Demo_Work%20bouquetman.mp4&quot;,
 &quot;poster&quot;:&quot;https:\\/\\/socigames.com\\/wp-content\\/uploads\\/x.jpg&quot;}'></div>
<video controls preload="none">
  <source src="https://fappingstream.com/Demo_Work%20bouquetman.mp4" />
</video>
<video autoplay loop muted playsinline style="width:300px;">
  <source src="https://socigames.com/wp-content/uploads/2026/01/banner_b.gif.mp4" type="video/mp4">
</video>
"""

BUNNY_PAGE = """
<title>Alpha &amp; Beta [NinNinja] &raquo; SOCIGAMES</title>
<video autoplay loop muted playsinline>
  <source src="https://socigames.com/wp-content/uploads/2026/02/banner_a.mp4" type="video/mp4">
</video>
<iframe allowfullscreen="true" class="perfmatters-lazy"
  data-src="https://iframe.mediadelivery.net/embed/100001/00000000-0000-4000-8000-000000000000?autoplay=false&amp;loop=true"></iframe>
"""


def test_direct_page_yields_only_the_offsite_source():
    got = parse_video_sources(DIRECT_PAGE, PAGE_URL)
    assert got == ["https://fappingstream.com/Demo_Work%20bouquetman.mp4"]


def test_ad_banners_are_never_returned():
    """Self-hosted banners outnumber the real video; picking one would
    download a 5-second advert in place of the requested clip."""
    for src in parse_video_sources(DIRECT_PAGE, PAGE_URL):
        assert "socigames.com" not in src


def test_bunny_page_has_no_progressive_source_but_yields_the_embed():
    assert parse_video_sources(BUNNY_PAGE, PAGE_URL) == []
    assert parse_bunny_embed(BUNNY_PAGE).startswith(
        "https://iframe.mediadelivery.net/embed/100001/")


def test_direct_page_has_no_bunny_embed():
    assert parse_bunny_embed(DIRECT_PAGE) == ""


@pytest.mark.parametrize("html,expected", [
    (DIRECT_PAGE, "Demo Work [Bouquetman]"),
    (BUNNY_PAGE, "Alpha & Beta [NinNinja]"),
    ("<title>No suffix here</title>", "No suffix here"),
    ("<html></html>", ""),
])
def test_page_title_strips_site_suffix(html, expected):
    assert parse_page_title(html) == expected


def test_select_by_resolution_prefers_exact_then_best():
    cands = [
        "https://cdn.example/clip_480p.mp4",
        "https://cdn.example/clip_1080p.mp4",
        "https://cdn.example/clip_720p.mp4",
    ]
    assert select_by_resolution(cands, "720").endswith("720p.mp4")
    assert select_by_resolution(cands, "best").endswith("1080p.mp4")
    # Unavailable height falls back to the best rather than failing.
    assert select_by_resolution(cands, "2160").endswith("1080p.mp4")
    assert select_by_resolution([], "best") == ""


@pytest.mark.parametrize("url,expected", [
    ("https://socigames.com/demo-work-bouquetman/", True),
    ("https://www.socigames.com/x/", True),
    ("https://cdn.socigames.com/x/", True),
    ("https://evil-socigames.com/x/", False),
    ("https://socigames.com.attacker.net/x/", False),
    ("https://hmvmania.com/x/", False),
])
def test_can_handle(url, expected):
    assert SociGamesProvider.can_handle(url) is expected


def test_generic_ytdlp_defers_to_this_provider():
    """Without the skip entry the generic provider wins on registry order and
    the page resolves to zero formats — the original failure."""
    from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
    assert YtdlpGenericProvider.can_handle(PAGE_URL) is False


def test_registry_routes_socigames():
    from funpairdl.providers.registry import ProviderRegistry
    assert ProviderRegistry().get_provider(PAGE_URL).name == "socigames"


def test_detect_provider_agrees_with_registry():
    from funpairdl.utils.url_parser import detect_provider
    assert detect_provider(PAGE_URL) == "socigames"
