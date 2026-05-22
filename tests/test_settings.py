"""Tests for Settings — script_variant_mode field."""
import json
import tempfile
from pathlib import Path

from funpairdl.persistence.settings import Settings


class TestSettings:
    def test_default_variant_mode(self):
        s = Settings()
        assert s.script_variant_mode == "flat"

    def test_roundtrip_save_load(self, tmp_path):
        config_path = tmp_path / "config.json"
        s = Settings(script_variant_mode="subfolder")
        s.save(config_path)

        loaded = Settings.load(config_path)
        assert loaded.script_variant_mode == "subfolder"

    def test_load_without_variant_mode(self, tmp_path):
        """Backward compat: old config without script_variant_mode."""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "download_dir": "G:\\Download",
            "max_segments": 8,
        }), encoding="utf-8")

        loaded = Settings.load(config_path)
        assert loaded.script_variant_mode == "flat"
