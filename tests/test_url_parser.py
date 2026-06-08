"""Tests for provider detection from a URL."""
from funpairdl.utils.url_parser import detect_provider


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

    def test_ytdlp_sites_and_direct_fallback(self):
        assert detect_provider("https://rule34video.com/video/1/x") == "ytdlp"
        assert detect_provider("https://www.bilibili.com/video/BV1") == "ytdlp"
        # Unknown host (artist site) → direct; yt-dlp's generic extractor is
        # tried at resolve time, but the label is "direct".
        assert detect_provider("https://artist-example.com/samplework/") == "direct"
        assert detect_provider("not a url") == "direct"
