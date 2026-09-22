"""hmvmania: the post's own mp4 wins; a related-video thumbnail named
"<video>.mp4_snapshot….jpg" is not a source."""
from funpairdl.providers.hmvmania import _select_by_resolution, parse_mp4_candidates

PAGE = """
<ul id="playlist1" style="display:none;">
  <li data-thumb-source="https://hmvmania.com/wp-content/uploads/2021/01/cover-80x80.jpg"
      data-video-source="https://hmvmania.com/wp-content/uploads/2021/01/DEMO04.mp4"></li>
</ul>
<li><a href="https://hmvmania.com/wp-content/uploads/2021/01/DEMO04.mp4" download>DL</a></li>
<div class="related-video-thumb">
  <img data-src="https://hmvmania.com/wp-content/uploads/2026/09/Newest-Post.mp4_snapshot_00.01.jpg">
  <a href="https://hmvmania.com/video/newest-post/">x</a>
</div>
<video src="https://hmvmania.com/wp-content/uploads/2021/01/Other-Clip_720p.mp4"></video>
"""


def test_thumbnail_names_are_not_candidates():
    cands = parse_mp4_candidates(PAGE, "https://hmvmania.com/video/demo-04/")
    assert "https://hmvmania.com/wp-content/uploads/2021/01/DEMO04.mp4" in cands
    assert not any("Newest-Post" in c for c in cands)


def test_own_video_is_listed_and_picked_first():
    cands = parse_mp4_candidates(PAGE, "https://hmvmania.com/video/demo-04/")
    assert cands[0].endswith("/DEMO04.mp4")
    # "best": both have no height (0) vs 720p → the 720p clip is higher, but
    # an exact-resolution request that no source meets falls back to the top
    # height; with equal heights the post's own source wins.
    assert _select_by_resolution(cands, "1080") == "https://hmvmania.com/wp-content/uploads/2021/01/Other-Clip_720p.mp4"
    same = ["https://hmvmania.com/wp-content/uploads/2021/01/DEMO04.mp4",
            "https://hmvmania.com/wp-content/uploads/2026/09/Other.mp4"]
    assert _select_by_resolution(same, "best") == same[0]
    assert _select_by_resolution(same, "1080") == same[0]
