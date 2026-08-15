"""Tests for API schemas — script_authors and probed sizes fields."""
from funpairdl.api.schemas import AddPairRequest, PairGroupSpec


class TestAddPairRequest:
    def test_script_authors_optional(self):
        req = AddPairRequest(name="Test", video_urls=["http://x/v.mp4"])
        assert req.script_authors is None

    def test_script_authors_provided(self):
        req = AddPairRequest(
            name="Test",
            script_urls=["http://x/a.funscript", "http://x/b.funscript"],
            script_authors={
                "http://x/a.funscript": "Alice",
                "http://x/b.funscript": "Bob",
            },
        )
        assert req.script_authors["http://x/a.funscript"] == "Alice"
        assert req.script_authors["http://x/b.funscript"] == "Bob"

    def test_from_json_without_script_authors(self):
        """Backward compat: old payloads without script_authors."""
        data = {"name": "Test", "video_urls": ["http://x/v.mp4"], "script_urls": []}
        req = AddPairRequest(**data)
        assert req.script_authors is None

    def test_from_json_with_script_authors(self):
        data = {
            "name": "Test",
            "script_urls": ["http://x/s.funscript"],
            "script_authors": {"http://x/s.funscript": "Author1"},
        }
        req = AddPairRequest(**data)
        assert req.script_authors == {"http://x/s.funscript": "Author1"}

    def test_sizes_optional(self):
        """Backward compat: payloads without sizes still validate."""
        req = AddPairRequest(name="Test", video_urls=["http://x/v.mp4"])
        assert req.sizes is None

    def test_sizes_provided(self):
        req = AddPairRequest(
            name="Test",
            video_urls=["http://x/v.mp4"],
            sizes={"http://x/v.mp4": 12345},
        )
        assert req.sizes == {"http://x/v.mp4": 12345}


class TestPairGroupSpec:
    def test_sizes_optional(self):
        grp = PairGroupSpec(name="Main", video_urls=["http://x/v.mp4"])
        assert grp.sizes is None

    def test_sizes_survive_model_dump(self):
        """routes.py forwards groups via model_dump() — sizes must ride along."""
        grp = PairGroupSpec(
            name="Alt 1",
            video_urls=["http://x/v.mp4"],
            sizes={"http://x/v.mp4": 999},
        )
        dumped = grp.model_dump()
        assert dumped["sizes"] == {"http://x/v.mp4": 999}
        assert dumped["name"] == "Alt 1"
