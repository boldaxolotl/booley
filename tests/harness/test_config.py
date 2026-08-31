"""Tests for configuration constants and registries in config.py."""

from __future__ import annotations

import pytest

from booley.config.settings import (
    _PROVIDER_TIER_MODELS,
    API_RETRY_BACKOFF_MULTIPLIER,
    API_RETRY_INITIAL_BACKOFF_S,
    API_RETRY_MAX_BACKOFF_S,
    HARD_MAX_COMPILE_RETRIES,
    HARD_MAX_DEBUG_ROUNDS,
    HARD_MAX_MUTATION_ITERATIONS,
    MAX_API_RETRIES,
    MAX_COMPILE_RETRIES,
    MAX_DEBUG_ROUNDS,
    MAX_FIX_LINES,
    MAX_MUTATION_ITERATIONS,
    MAX_POST_REVIEW_SIM_ITERATIONS,
    MODEL_MAP,
    STEP_TIERS,
    SandboxConfig,
    _parse_sandbox_config,
)

# Derived, not hardcoded: a second copy of the model list only ever rots out of
# sync at the next model launch. The invariant worth testing is that MODEL_MAP
# names models some provider actually offers.
KNOWN_CLAUDE_MODELS = set(_PROVIDER_TIER_MODELS["claude"].values())
KNOWN_CODEX_MODELS = set(_PROVIDER_TIER_MODELS["codex"].values())
ALL_KNOWN_MODELS = KNOWN_CLAUDE_MODELS | KNOWN_CODEX_MODELS


class TestHardCaps:
    def test_hard_caps_ge_defaults(self):
        assert HARD_MAX_DEBUG_ROUNDS >= MAX_DEBUG_ROUNDS
        assert HARD_MAX_COMPILE_RETRIES >= MAX_COMPILE_RETRIES
        assert HARD_MAX_MUTATION_ITERATIONS >= MAX_MUTATION_ITERATIONS

    @pytest.mark.parametrize(
        "cap",
        [
            HARD_MAX_DEBUG_ROUNDS,
            HARD_MAX_COMPILE_RETRIES,
            HARD_MAX_MUTATION_ITERATIONS,
        ],
    )
    def test_hard_caps_positive_int(self, cap):
        assert isinstance(cap, int)
        assert cap > 0


class TestModelMap:
    def test_not_empty(self):
        assert len(MODEL_MAP) > 0

    def test_default_values_from_known_set(self):
        for _key, val in MODEL_MAP.items():
            assert val in ALL_KNOWN_MODELS

    @pytest.mark.parametrize("key", ["developer", "recovery"])
    def test_key_entries_exist(self, key):
        assert key in MODEL_MAP


class TestProviderTierModels:
    def test_both_providers_defined(self):
        assert "claude" in _PROVIDER_TIER_MODELS
        assert "codex" in _PROVIDER_TIER_MODELS

    @pytest.mark.parametrize("provider", ["claude", "codex"])
    def test_all_tiers_present(self, provider):
        tiers = _PROVIDER_TIER_MODELS[provider]
        for t in ("heavy", "standard", "light"):
            assert t in tiers

    def test_step_tiers_cover_model_map(self):
        for key in MODEL_MAP:
            assert key in STEP_TIERS


class TestAPIResilience:
    def test_backoff_multiplier_gt_one(self):
        assert API_RETRY_BACKOFF_MULTIPLIER > 1

    def test_initial_lt_max_backoff(self):
        assert API_RETRY_INITIAL_BACKOFF_S < API_RETRY_MAX_BACKOFF_S

    def test_retries_positive(self):
        assert MAX_API_RETRIES > 0


class TestLimits:
    @pytest.mark.parametrize(
        "limit",
        [
            MAX_DEBUG_ROUNDS,
            MAX_COMPILE_RETRIES,
            MAX_MUTATION_ITERATIONS,
            MAX_POST_REVIEW_SIM_ITERATIONS,
            MAX_FIX_LINES,
        ],
    )
    def test_positive_int(self, limit):
        assert isinstance(limit, int)
        assert limit > 0


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

from booley.config.settings import (
    DEFAULT_DEVELOPER_ACTIVE_TIMEOUT_S,
    DEFAULT_DEVELOPER_WALL_TIMEOUT_S,
    DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S,
    DEFAULT_INTERACTIVE_MAX_SESSIONS,
    DeveloperLimitsConfig,
    InteractiveConfig,
    load_developer_limits_config,
    load_interactive_config,
)


def _write_toml(tmp_path, body: str):
    proj = tmp_path / ".booley_project"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "booley.toml").write_text(body, encoding="utf-8")
    return tmp_path


class TestInteractiveConfig:
    def test_defaults_when_section_absent(self, tmp_path):
        _write_toml(tmp_path, "[project]\nname='x'\n")
        cfg = load_interactive_config(tmp_path)
        assert cfg == InteractiveConfig()
        assert cfg.idle_timeout_seconds == DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S
        assert cfg.max_sessions == DEFAULT_INTERACTIVE_MAX_SESSIONS
        assert cfg.egress_allowlist == ()

    def test_defaults_when_no_toml(self, tmp_path):
        assert load_interactive_config(tmp_path) == InteractiveConfig()

    def test_project_values_are_not_adopted_as_host_policy(self, tmp_path):
        _write_toml(
            tmp_path,
            "[interactive]\n"
            "idle_timeout_seconds = 600\n"
            "max_sessions = 2\n"
            "egress_allowlist = ['ex.com', 'foo.test']\n",
        )
        cfg = load_interactive_config(tmp_path)
        assert cfg == InteractiveConfig()

    def test_rejects_nonpositive(self, tmp_path):
        _write_toml(tmp_path, "[interactive]\nidle_timeout_seconds = 0\nmax_sessions = -3\n")
        cfg = load_interactive_config(tmp_path)
        assert cfg.idle_timeout_seconds == DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S
        assert cfg.max_sessions == DEFAULT_INTERACTIVE_MAX_SESSIONS

    def test_rejects_wrong_types(self, tmp_path):
        _write_toml(
            tmp_path, "[interactive]\nidle_timeout_seconds = 'soon'\negress_allowlist = 'ex.com'\n"
        )
        cfg = load_interactive_config(tmp_path)
        assert cfg.idle_timeout_seconds == DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S
        assert cfg.egress_allowlist == ()

    def test_bool_is_not_accepted_as_int(self, tmp_path):
        _write_toml(tmp_path, "[interactive]\nmax_sessions = true\n")
        assert load_interactive_config(tmp_path).max_sessions == DEFAULT_INTERACTIVE_MAX_SESSIONS


class TestDeveloperLimitsConfig:
    def test_defaults(self, tmp_path):
        cfg = load_developer_limits_config(tmp_path)
        assert cfg == DeveloperLimitsConfig()
        assert cfg.active_timeout_seconds == DEFAULT_DEVELOPER_ACTIVE_TIMEOUT_S
        assert cfg.wall_timeout_seconds == DEFAULT_DEVELOPER_WALL_TIMEOUT_S

    def test_reads_overrides(self, tmp_path):
        _write_toml(
            tmp_path,
            "[developer.limits]\nactive_timeout_seconds = 2400\nwall_timeout_seconds = 86400\n",
        )
        cfg = load_developer_limits_config(tmp_path)
        assert cfg.active_timeout_seconds == 2400
        assert cfg.wall_timeout_seconds == 86400

    def test_invalid_values_use_defaults(self, tmp_path):
        _write_toml(
            tmp_path,
            "[developer.limits]\nactive_timeout_seconds = 0\nwall_timeout_seconds = 'forever'\n",
        )
        assert load_developer_limits_config(tmp_path) == DeveloperLimitsConfig()

    def test_wall_may_be_below_active(self, tmp_path):
        _write_toml(
            tmp_path,
            "[developer.limits]\nactive_timeout_seconds = 100\nwall_timeout_seconds = 50\n",
        )
        cfg = load_developer_limits_config(tmp_path)
        assert cfg.active_timeout_seconds == 100
        assert cfg.wall_timeout_seconds == 50
