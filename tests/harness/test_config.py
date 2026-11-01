"""Tests for configuration constants and registries in config.py."""

from __future__ import annotations

import pytest

from booley.config.settings import (
    _PROVIDER_TIER_MODELS,
    SandboxConfig,
    _parse_sandbox_config,
)
from booley.runtime._retry import (
    API_RETRY_BACKOFF_MULTIPLIER,
    API_RETRY_INITIAL_BACKOFF_S,
    API_RETRY_MAX_BACKOFF_S,
    MAX_API_RETRIES,
)


class TestProviderTierModels:
    def test_both_providers_defined(self):
        assert "claude" in _PROVIDER_TIER_MODELS
        assert "codex" in _PROVIDER_TIER_MODELS

    @pytest.mark.parametrize("provider", ["claude", "codex"])
    def test_all_tiers_present(self, provider):
        tiers = _PROVIDER_TIER_MODELS[provider]
        for t in ("heavy", "standard", "light"):
            assert t in tiers


class TestAPIResilience:
    def test_backoff_multiplier_gt_one(self):
        assert API_RETRY_BACKOFF_MULTIPLIER > 1

    def test_initial_lt_max_backoff(self):
        assert API_RETRY_INITIAL_BACKOFF_S < API_RETRY_MAX_BACKOFF_S

    def test_retries_positive(self):
        assert MAX_API_RETRIES > 0


class TestSandboxConfig:
    def test_defaults(self):
        cfg = SandboxConfig()
        assert cfg.image == "booley-sandbox"
        # ADR 0028: one container memory limit; empty = no explicit limit
        # (matches the pre-0028 devcontainer default).
        assert cfg.memory == ""


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

    def test_unknown_retired_mode_is_ignored(self):
        data = {"sandbox": {"mode": "podman"}}
        cfg = _parse_sandbox_config(data)
        assert cfg.image == "booley-sandbox"


# ===========================================================================
# [interactive] config (ADR 0018)
# ===========================================================================
