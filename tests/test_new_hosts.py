"""New hosts (The JOI Database, WatchHentai, pixivFANBOX, PMVHaven, Faptap,
MediaFire): the page parsing their providers and probes share. Synthetic pages; no network."""
import base64

from funpairdl.providers.joidb import (
    JoiDbProvider, estimate_size, parse_title as joi_title, parse_variants,
    playlist_duration, select_variant, video_id,
)
from funpairdl.providers.watchhentai import (
    WatchHentaiProvider, build_filename, decode_source, parse_duration,
    parse_player_url, parse_sources, parse_title as wh_title, select_source,
)
from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
from funpairdl.utils.url_parser import detect_provider


def _encode(url: str) -> str:
    """Inverse of the player's decoder (for building fixtures)."""
    inner = base64.b64encode(url.encode()).decode()[::-1]
    x = bytes(ord(c) ^ ((13 + i % 17) & 255) for i, c in enumerate(inner))
    return base64.b64encode(x).decode().replace("+", "-").replace("/", "_").rstrip("=")


class TestJoiDb:
    WATCH = "https://www.the-joi-database.com/watch/0123456789abcdef01234567"

    def test_routing(self):
        assert JoiDbProvider.can_handle(self.WATCH)
        assert not JoiDbProvider.can_handle("https://www.the-joi-database.com/videos?search=x")
        assert not YtdlpGenericProvider.can_handle(self.WATCH)
        assert detect_provider(self.WATCH) == "joidb"
        assert video_id(self.WATCH) == "0123456789abcdef01234567"

    def test_title_from_download_button(self):
        html = ('<title>Garden Party (JOI) - The joi Database</title>'
                '<a data-video-title="Garden Party (JOI).mp4" data-video-id="x">')
        assert joi_title(html) == "Garden Party (JOI)"
        assert joi_title("<title>Garden Party - The joi Database</title>") == "Garden Party"

    def test_variants_and_pick(self):
        master = ('#EXTM3U\n#EXT-X-VERSION:3\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,NAME="720"\n'
                  'video_x_720p.m3u8\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=640x360,NAME="360"\n'
                  'video_x_360p.m3u8\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,NAME="1080"\n'
                  'video_x_1080p.m3u8\n')
        v = parse_variants(master, "https://www.the-joi-database.com/api/stream/x")
        assert [x["height"] for x in v] == [360, 720, 1080]
        assert v[2]["url"] == "https://www.the-joi-database.com/api/stream/video_x_1080p.m3u8"
        assert select_variant(v, "720")["height"] == 720
        assert select_variant(v, "480")["height"] == 1080
        assert select_variant(v, "best")["height"] == 1080
        assert estimate_size(v[2], 100.0) == 50_000_000

    def test_playlist_duration(self):
        pl = "#EXTM3U\n#EXTINF:10.5,\na.ts\n#EXTINF:4.5,\nb.ts\n#EXT-X-ENDLIST\n"
        assert playlist_duration(pl) == 15.0


class TestWatchHentai:
    PAGE = "https://watchhentai.net/videos/garden-party-episode-2-id-01/"

    def test_routing(self):
        assert WatchHentaiProvider.can_handle(self.PAGE)
        assert not WatchHentaiProvider.can_handle("https://watchhentai.net/series/garden-party/")
        assert not YtdlpGenericProvider.can_handle(self.PAGE)
        assert detect_provider(self.PAGE) == "watchhentai"

    def test_decoder_roundtrip(self):
        url = "https://storage.example/files/G/garden-party/garden-party-2_1080p.mp4"
        assert decode_source(_encode(url)) == url

    def test_player_and_sources(self):
        page = '<iframe data-primary-player-url="https://watchhentai.net/player/1/1/mp4/" data-x="y">'
        assert parse_player_url(page, self.PAGE) == "https://watchhentai.net/player/1/1/mp4/"
        a = "https://storage.example/files/G/garden-party/garden-party-2_1080p.mp4"
        b = "https://storage.example/files/G/garden-party/garden-party-2_720p.mp4"
        player = ('var whJwSources = [{"file":"%s","type":"video\\/mp4","label":"1080p"},'
                  '{"file":"%s","type":"video\\/mp4","label":"720p"}];\nwhJwSources.forEach(f);'
                  % (_encode(a), _encode(b)))
        src = parse_sources(player)
        assert [s["height"] for s in src] == [720, 1080]
        assert src[1]["url"] == a
        assert select_source(src, "720")["url"] == b
        assert select_source(src, "best")["url"] == a

    def test_title_duration_filename(self):
        html = ('<title>Garden Party - Episode 2 - Watch Hentai, Stream Online English Subbed</title>'
                '<meta itemprop="duration" content="PT16M30S" />')
        assert wh_title(html) == "Garden Party - Episode 2"
        assert parse_duration(html) == 990.0
        assert parse_duration("<p>none</p>") == 0.0
        assert build_filename("Garden Party - Episode 2", "https://s/x.mp4") == "Garden Party - Episode 2.mp4"
        assert build_filename("", "https://s/files/garden-party-2_1080p.mp4") == "garden-party-2_1080p.mp4"


class TestFanbox:
    def test_routing(self):
        from funpairdl.providers.fanbox import FanboxProvider, post_id
        post = "https://somecreator.fanbox.cc/posts/1234567"
        assert FanboxProvider.can_handle(post)
        assert post_id(post) == "1234567"
        assert not FanboxProvider.can_handle("https://somecreator.fanbox.cc/")
        assert not FanboxProvider.can_handle("https://downloads.fanbox.cc/files/post/1/abc.mp4")
        assert not YtdlpGenericProvider.can_handle(post)
        assert detect_provider(post) == "fanbox"

    def test_files_and_names(self):
        from funpairdl.providers.fanbox import file_name, paid_error, video_files
        post = {"title": "Garden Party", "body": {
            "files": [{"name": "Censored", "extension": "mp4", "size": 10, "url": "https://d/a.mp4"},
                      {"name": "cover", "extension": "png", "size": 1, "url": "https://d/c.png"}],
            "fileMap": {"x": {"name": "Uncensored", "extension": "mp4", "size": 20, "url": "https://d/b.mp4"}},
        }}
        vids = video_files(post)
        assert [v["name"] for v in vids] == ["Censored", "Uncensored"]
        assert file_name(post, vids[0], False) == "Garden Party.mp4"
        assert file_name(post, vids[1], True) == "Garden Party - Uncensored.mp4"
        assert "¥300" in paid_error(300) and paid_error(300).startswith("Paid content")


class TestPmvHaven:
    PAGE = "https://pmvhaven.com/video/garden-party_0123456789abcdef01234567"

    def test_routing(self):
        from funpairdl.providers.pmvhaven import PmvHavenProvider
        assert PmvHavenProvider.can_handle(self.PAGE)
        assert not PmvHavenProvider.can_handle("https://pmvhaven.com/profile/someone")
        assert not YtdlpGenericProvider.can_handle(self.PAGE)
        assert detect_provider(self.PAGE) == "pmvhaven"

    def test_master_and_original(self):
        from funpairdl.providers.pmvhaven import original_url, parse_master, parse_title, pick
        esc = chr(92) + "u002F"
        html = ('<title>Garden Party - PMVHaven</title><script>window.__NUXT__=["'
                + "https:" + esc * 2 + "cloud.example" + esc + "videos" + esc
                + 'someone_-_Garden_Party_17_abc.mp4' + esc + 'master.m3u8"]</script>')
        m = parse_master(html)
        assert m == "https://cloud.example/videos/someone_-_Garden_Party_17_abc.mp4/master.m3u8"
        assert original_url(m) == "https://cloud.example/videos/someone_-_Garden_Party_17_abc.mp4"
        assert parse_title(html) == "Garden Party"
        v = [{"height": 720, "bandwidth": 1}, {"height": 1080, "bandwidth": 2}, {"height": 2160, "bandwidth": 9}]
        assert pick(v, "1080")["height"] == 1080
        assert pick(v, "2160") is None      # the top height is the original upload
        assert pick(v, "best") is None
        assert pick(v, "480") is None


class TestFaptap:
    def test_routing_and_sources(self):
        from funpairdl.providers.faptap import FaptapProvider, parse_sources, select_source, video_id
        url = "https://faptap.net/v/1234567890123456789"
        assert FaptapProvider.can_handle(url) and video_id(url) == "1234567890123456789"
        assert not YtdlpGenericProvider.can_handle(url)
        src = parse_sources([
            {"url": "stream?s=a", "quality": "720", "format": "mp4"},
            {"url": "stream?s=b", "quality": "480", "format": "mp4"},
            {"url": "stream?s=c", "quality": "1080", "format": "m3u8"},
        ])
        assert [s["height"] for s in src] == [480, 720]
        assert src[1]["url"] == "https://faptap.net/api/stream?s=a"
        assert select_source(src, "480")["height"] == 480
        assert select_source(src, "1080")["height"] == 720


class TestMediafire:
    def test_routing(self):
        from funpairdl.core.queue_manager import QueueManager
        from funpairdl.providers.mediafire import MediafireProvider, file_key, folder_key, is_folder_url
        f = "https://www.mediafire.com/file/abc123def456/Garden_Party.mp4/file"
        d = "https://www.mediafire.com/folder/zyx987/Garden_Party"
        assert MediafireProvider.can_handle(f) and file_key(f) == "abc123def456"
        assert MediafireProvider.can_handle("https://www.mediafire.com/file_premium/abc123def456/x.mp4/file")
        assert not MediafireProvider.can_handle(d)
        assert is_folder_url(d) and folder_key(d) == "zyx987"
        assert QueueManager._is_bundle_url(d) and not QueueManager._is_bundle_url(f)
        assert not YtdlpGenericProvider.can_handle(f)
        assert detect_provider(d) == "mediafire"

    def test_download_button(self):
        from funpairdl.providers.mediafire import name_from_link, parse_download_link
        html = ('<a class="input popsok" aria-label="Download file" '
                'href="https://download1234.mediafire.com/tok/abc123def456/Garden+Party.mp4" '
                'id="downloadButton" rel="nofollow">')
        link = parse_download_link(html)
        assert link == "https://download1234.mediafire.com/tok/abc123def456/Garden+Party.mp4"
        assert name_from_link(link) == "Garden Party.mp4"
        assert parse_download_link("<p>nothing</p>") == ""


class TestVikingFile:
    def test_routing_and_page(self):
        from funpairdl.providers.vikingfile import VikingFileProvider, parse_page
        page = "https://vik1ngfile.site/f/AbCdEf1234"
        assert VikingFileProvider.can_handle(page)
        assert VikingFileProvider.can_handle("https://vikingfile.com/f/AbCdEf1234")
        assert not VikingFileProvider.can_handle("https://vikingfile.com/d/AbCd/x.mp4")
        assert not YtdlpGenericProvider.can_handle(page)
        assert detect_provider(page) == "vikingfile"
        info = parse_page('<title>Garden_Party.mp4</title><a href="https://u.example/download/'
                          'Garden_Party.mp4%20%5B1.37%20GB%5D">Download via Usenet</a>')
        assert info["name"] == "Garden_Party.mp4"
        assert info["size"] == int(1.37 * 1024 ** 3)


class TestBrowserAssist:
    def test_link_comes_back_from_another_thread(self):
        import asyncio
        import threading
        from funpairdl.core.browser_assist import BrowserAssist

        ba = BrowserAssist()
        seen = []

        def handler(req):
            seen.append(req)
            threading.Timer(0.05, lambda: ba.complete(req["id"], {"url": "https://h/d/x.mp4", "name": "x.mp4"})).start()

        ba.set_handler(handler)
        got = asyncio.run(ba.open("https://vik1ngfile.site/f/abc", "vikingfile", timeout=5))
        assert got["url"] == "https://h/d/x.mp4"
        assert seen[0]["site"] == "vikingfile" and "extract_js" in seen[0]

    def test_cancel_and_no_gui(self):
        import asyncio
        import pytest
        from funpairdl.core.browser_assist import BrowserAssist

        ba = BrowserAssist()
        with pytest.raises(ValueError, match="embedded browser"):
            asyncio.run(ba.open("https://vik1ngfile.site/f/abc", "vikingfile"))
        ba.set_handler(lambda req: ba.complete(req["id"], None, "Browser check cancelled by the user"))
        with pytest.raises(ValueError, match="cancelled"):
            asyncio.run(ba.open("https://vik1ngfile.site/f/abc", "vikingfile", timeout=5))
