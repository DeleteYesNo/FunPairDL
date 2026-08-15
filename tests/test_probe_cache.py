"""Tests for the probe TTL cache and pure meta-derivation logic."""
import pytest

from funpairdl.constants import PROBE_CACHE_TTL_SECONDS
from funpairdl.providers.probe import (
    ProbeMeta,
    _CACHE_MAX_ENTRIES,
    _cache_get,
    _cache_put,
    _is_fruitful,
    _meta_from_info,
    clear_probe_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_probe_cache()
    yield
    clear_probe_cache()


class TestIsFruitful:
    def test_failure_not_fruitful(self):
        assert not _is_fruitful({"success": False, "error": "boom"})

    def test_size_zero_no_filename_not_fruitful(self):
        # e.g. the eroscripts skip result — must not be cached
        assert not _is_fruitful({"success": True, "provider": "eroscripts", "size": 0})

    def test_size_makes_fruitful(self):
        assert _is_fruitful({"success": True, "provider": "direct", "size": 123})

    def test_filename_makes_fruitful(self):
        assert _is_fruitful({"success": True, "provider": "ytdlp", "filename": "t"})

    def test_missing_size_key_with_filename(self):
        assert _is_fruitful({"success": True, "filename": "a.mp4"})


class TestCacheTtl:
    def test_put_then_get(self):
        r = {"success": True, "provider": "pixeldrain", "size": 10, "filename": "a"}
        _cache_put("u1", r, now=100.0)
        assert _cache_get("u1", now=100.0 + PROBE_CACHE_TTL_SECONDS - 1) is r

    def test_expired_entry_evicted(self):
        r = {"success": True, "size": 10, "filename": "a"}
        _cache_put("u1", r, now=100.0)
        assert _cache_get("u1", now=100.0 + PROBE_CACHE_TTL_SECONDS) is None
        # And it was actually removed, not just hidden
        assert _cache_get("u1", now=100.0) is None

    def test_unfruitful_not_cached(self):
        _cache_put("u1", {"success": True, "size": 0, "filename": ""}, now=100.0)
        assert _cache_get("u1", now=100.0) is None
        _cache_put("u2", {"success": False, "error": "x"}, now=100.0)
        assert _cache_get("u2", now=100.0) is None

    def test_miss_returns_none(self):
        assert _cache_get("nope", now=0.0) is None

    def test_cap_evicts_oldest_inserted(self):
        for i in range(_CACHE_MAX_ENTRIES):
            _cache_put(f"u{i}", {"success": True, "size": i + 1}, now=100.0)
        # All present at cap
        assert _cache_get("u0", now=101.0) is not None
        # One more insert evicts the oldest-inserted entry (u0)
        _cache_put("extra", {"success": True, "size": 1}, now=101.0)
        assert _cache_get("u0", now=101.0) is None
        assert _cache_get("extra", now=101.0) is not None
        assert _cache_get(f"u{_CACHE_MAX_ENTRIES - 1}", now=101.0) is not None

    def test_put_prunes_expired_before_capping(self):
        for i in range(_CACHE_MAX_ENTRIES):
            _cache_put(f"u{i}", {"success": True, "size": i + 1}, now=100.0)
        later = 100.0 + PROBE_CACHE_TTL_SECONDS + 1
        _cache_put("fresh", {"success": True, "size": 5}, now=later)
        assert _cache_get("fresh", now=later) is not None
        # Expired bulk is gone
        assert _cache_get("u3", now=later) is None


class TestMetaFromInfo:
    def test_failure_gives_empty_meta(self):
        assert _meta_from_info({"success": False, "error": "x"}) == ProbeMeta()
        assert _meta_from_info({}) == ProbeMeta()

    def test_direct_size(self):
        m = _meta_from_info({"success": True, "provider": "pixeldrain",
                             "size": 42, "filename": "a.mp4"})
        assert m == ProbeMeta(size=42, filename="a.mp4", source="pixeldrain")

    def test_direct_provider_maps_to_head_source(self):
        m = _meta_from_info({"success": True, "provider": "direct", "size": 7})
        assert m.source == "head"
        assert m.size == 7

    def test_format_size_fallback_uses_max_height(self):
        info = {
            "success": True, "provider": "ytdlp", "filename": "title",
            "formats": [
                {"height": 720, "size": 100},
                {"height": 1080, "size": 200},
                {"height": 2160, "size": 0},   # no size — skipped
            ],
        }
        m = _meta_from_info(info)
        assert m.size == 200
        # yt-dlp "filenames" are bare titles with no extension — they must
        # NOT be persisted as item filenames (resolve names the file later).
        assert m.filename == ""
        assert m.source == "ytdlp"

    def test_no_sized_formats_gives_zero(self):
        info = {"success": True, "provider": "ytdlp", "filename": "t",
                "formats": [{"height": 720, "size": 0}]}
        assert _meta_from_info(info).size == 0

    def test_multi_file_bundle_placeholder_name_blanked(self):
        info = {"success": True, "provider": "gofile", "size": 300,
                "filename": "3 files",
                "files": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        m = _meta_from_info(info)
        assert m.filename == ""
        assert m.size == 300

    def test_single_file_bundle_keeps_real_name(self):
        info = {"success": True, "provider": "gofile", "size": 50,
                "filename": "real.mp4", "files": [{"name": "real.mp4"}]}
        assert _meta_from_info(info).filename == "real.mp4"
