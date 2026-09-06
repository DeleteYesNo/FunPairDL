"""Tests for provider detection from a URL."""
from funpairdl.utils.url_parser import detect_provider, source_label


class TestDetectProvider:
    def test_vk_routes_to_ytdlp(self):
        # VK serves HLS/DASH; it must go to yt-dlp, not the direct segment
        # downloader (which gets HTTP 400 from VK's okcdn CDN).
        assert detect_provider("https://m.vk.com/video-111222333_444555666") == "ytdlp"
        assert detect_provider("https://vk.com/video-1_2") == "ytdlp"
        assert detect_provider("https://vkvideo.ru/video-1_2") == "ytdlp"

    def test_specialized_providers(self):
        assert detect_provider("https://pixeldrain.com/u/abc") == "pixeldrain"
        assert detect_provider("https://mega.nz/file/abc#k") == "mega"
        assert detect_provider("https://www.iwara.tv/video/x/slug") == "iwara"
        assert detect_provider(
            "https://discuss.eroscripts.com/uploads/short-url/x.funscript"
        ) == "eroscripts"
        assert detect_provider("https://e621.net/posts/1234567?q=x") == "e621"
        assert detect_provider("https://e926.net/posts/1") == "e621"

    def test_ytdlp_sites_and_direct_fallback(self):
        assert detect_provider("https://rule34video.com/video/1/x") == "ytdlp"
        assert detect_provider("https://www.bilibili.com/video/BV1") == "ytdlp"
        # Unknown host (artist site) → direct; yt-dlp's generic extractor is
        # tried at resolve time, but the label is "direct".
        assert detect_provider("https://artist-example.com/samplework/") == "direct"
        assert detect_provider("not a url") == "direct"


class TestSourceLabel:
    def test_brand_names_for_dedicated_providers(self):
        assert source_label("pixeldrain", "https://pixeldrain.com/u/abc") == "Pixeldrain"
        assert source_label("mega", "https://mega.nz/file/abc#k") == "MEGA"
        assert source_label("e621", "https://e621.net/posts/1234567") == "e621"
        assert source_label("gofile", "https://gofile.io/d/abc") == "GoFile"

    def test_catch_all_providers_show_host(self):
        assert source_label("ytdlp", "https://rule34video.com/video/1/x") == "rule34video.com"
        assert source_label("ytdlp", "https://www.pornhub.com/view_video.php?v=1") == "pornhub.com"
        assert source_label("direct", "https://artist-example.com/work.mp4") == "artist-example.com"

    def test_legacy_direct_tag_is_rederived_from_url(self):
        # Items queued before a site got its own provider carry "direct".
        assert source_label("direct", "https://e621.net/posts/1234567?q=x") == "e621"
        assert source_label("", "https://pixeldrain.com/u/abc") == "Pixeldrain"

    def test_forum_upload_cdn_reads_as_eroscripts(self):
        assert source_label(
            "direct", "https://eroscripts-discourse.eroscripts.com/original/4X/a/b.funscript"
        ) == "EroScripts"
        assert source_label(
            "eroscripts", "https://discuss.eroscripts.com/uploads/short-url/x.funscript"
        ) == "EroScripts"

    def test_garbage_url_does_not_raise(self):
        assert source_label("direct", "not a url") == ""
