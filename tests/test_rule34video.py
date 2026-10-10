"""Rule34Video: renditions come from the page's KVS player flashvars."""
from funpairdl.providers.rule34video import (
    Rule34VideoProvider, parse_duration, parse_sources, parse_title, select_source,
)
from funpairdl.providers.registry import ProviderRegistry
from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
from funpairdl.utils.url_parser import detect_provider

PAGE = """<html><head><title>Sample Work | Mock PMV (Contest)</title>
<script type="application/ld+json">{"@type": "VideoObject", "duration": "PT0H3M05S"}</script></head>
<body><script>var flashvars = {
  video_id: '100', license_code: '$000', postfix: '_360.mp4',
  video_url: 'https://rule34video.com/get_file/1/aa/100000/100/100_360.mp4/?v-acctoken=t1',
  video_url_text: '360p',
  video_alt_url: 'https://rule34video.com/get_file/1/bb/100000/100/100_480p.mp4/?v-acctoken=t2',
  video_alt_url_text: '480p',
  video_alt_url2: 'https://rule34video.com/get_file/1/cc/100000/100/100_1080p.mp4/?v-acctoken=t3',
  video_alt_url2_text: '1080p', video_alt_url2_hd: '1',
  video_alt_url3: 'https://rule34video.com/get_file/1/dd/100000/100/100_2160p.mp4/?v-acctoken=t4',
  video_alt_url3_text: '4K'
};</script></body></html>"""


def test_parse_sources_lists_every_rendition_lowest_first():
    srcs = parse_sources(PAGE)
    assert [s["height"] for s in srcs] == [360, 480, 1080, 2160]
    assert srcs[0]["url"].endswith("100_360.mp4/?v-acctoken=t1")


def test_parse_duration_and_title():
    assert parse_duration(PAGE) == 185
    assert parse_duration("<html></html>") is None
    assert parse_title(PAGE) == "Sample Work | Mock PMV (Contest)"


def test_select_source_prefers_the_asked_height_else_the_best():
    srcs = parse_sources(PAGE)
    assert select_source(srcs, "1080")["height"] == 1080
    assert select_source(srcs, "720")["height"] == 2160
    assert select_source(srcs, "best")["height"] == 2160
    assert select_source([], "1080") is None


def test_routing_goes_to_the_dedicated_provider_not_ytdlp():
    url = "https://rule34video.com/video/100/sample-work-mock-pmv/"
    assert detect_provider(url) == "rule34video"
    assert Rule34VideoProvider.can_handle(url)
    assert not YtdlpGenericProvider.can_handle(url)
    assert ProviderRegistry().get_provider(url).name == "rule34video"
