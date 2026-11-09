"""Tests for ``[sandbox]`` parsing in booley.toml."""

from __future__ import annotations

import pytest

from booley.config.settings import _parse_sandbox_config


class TestParseSandboxConfig:
    def test_empty_data(self):
        cfg = _parse_sandbox_config({})
        assert cfg.memory == ""
        assert cfg.mount_host_skills is False

    def test_mount_host_skills_opt_in(self):
        cfg = _parse_sandbox_config({"sandbox": {"mount_host_skills": True}})
        assert cfg.mount_host_skills is True

    def test_retired_mode_is_ignored(self):
        data = {"sandbox": {"mode": "docker"}}
        cfg = _parse_sandbox_config(data)
        assert cfg.image == "booley-sandbox"

    def test_custom_image(self):
        data = {"sandbox": {"image": "ibex-booley-sandbox:latest"}}
        cfg = _parse_sandbox_config(data)
        assert cfg.image == "ibex-booley-sandbox:latest"

    @pytest.mark.parametrize("image", ["", 123])
    def test_invalid_image_falls_back(self, image):
        data = {"sandbox": {"image": image}}
        cfg = _parse_sandbox_config(data)
        assert cfg.image == "booley-sandbox"

    def test_memory_string(self):
        data = {"sandbox": {"mode": "docker", "memory": "8g"}}
        cfg = _parse_sandbox_config(data)
        assert cfg.memory == "8g"

    def test_memory_legacy_tier_table_warns_and_keeps_default(self, caplog):
        """Retired tier table: warn, salvage the 'default' entry, drop tiers."""
        data = {
            "sandbox": {
                "mode": "docker",
                "memory": {"default": "6g", "agent": "8g"},
            },
        }
        with caplog.at_level("WARNING"):
            cfg = _parse_sandbox_config(data)
        assert cfg.memory == "6g"
        assert any("retired" in r.message for r in caplog.records)

    def test_memory_tiers_key_warns_and_is_ignored(self, caplog):
        data = {"sandbox": {"memory_tiers": {"sim": "4g"}}}
        with caplog.at_level("WARNING"):
            cfg = _parse_sandbox_config(data)
        assert cfg.memory == ""
        assert any("memory_tiers is retired" in r.message for r in caplog.records)
