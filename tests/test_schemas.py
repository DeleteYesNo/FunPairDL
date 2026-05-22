"""Tests for API schemas — script_authors field."""
from funpairdl.api.schemas import AddPairRequest


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
