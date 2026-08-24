"""Tests for [models.roles] — per-agent model pins.

Covers the knob end to end: parsing/validation, resolution precedence in
BackendConfig, the two consumer paths (harness steps via MODEL_MAP, specialist
subprocesses via the lazy config), and doctor's schema checks.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit import project_schema
from booley.config import agent as bc
from booley.config.agent import (
    _KNOWN_ROLES,
    _MODEL_TIERS,
    BackendConfig,
    BackendConfigError,
    _parse_role_models,
    _parse_tier_models,
    _resolve_tier_models,
    load_models_config,
)
from booley.config.settings import MODEL_MAP


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate from ambient harness hand-offs and any installed global config."""
    saved_model_map = dict(MODEL_MAP)
    for var in (
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PROJECT_DIR",
        "BOOLEY_AGENT_APP",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(bc, "_backend_config", None)
    monkeypatch.setattr(bc, "_inside_container", lambda: False)
    yield
    MODEL_MAP.clear()
    MODEL_MAP.update(saved_model_map)


def _write_toml(root: Path, body: str) -> Path:
    """Create a project whose .booley_project/booley.toml holds *body*."""
    project_data = root / ".booley_project"
    project_data.mkdir(parents=True, exist_ok=True)
    (project_data / "booley.toml").write_text(body)
    return root


# --- parsing -----------------------------------------------------------------


class TestParseRoleModels:
    def test_absent_roles_table_is_empty(self):
        assert _parse_role_models({}) == {}
        assert _parse_role_models({"heavy": "claude-opus-4-8"}) == {}

    def test_parses_every_known_role(self):
        section = {"roles": dict.fromkeys(_KNOWN_ROLES, "claude-opus-4-8")}
        assert _parse_role_models(section) == dict.fromkeys(_KNOWN_ROLES, "claude-opus-4-8")

    def test_values_are_stripped(self):
        assert _parse_role_models({"roles": {"reviewer": "  light  "}}) == {"reviewer": "light"}

    def test_unknown_role_raises(self):
        # The whole point of the knob is that a pin is honored; a typo that
        # silently bills a different model is the failure mode to prevent.
        with pytest.raises(BackendConfigError, match="unknown role 'reviwer'"):
            _parse_role_models({"roles": {"reviwer": "claude-opus-4-8"}})

    @pytest.mark.parametrize("bad", [42, "", "   ", None, ["claude-opus-4-8"]])
    def test_non_string_or_empty_value_raises(self, bad):
        with pytest.raises(BackendConfigError, match="is invalid"):
            _parse_role_models({"roles": {"reviewer": bad}})

    def test_roles_not_a_table_raises(self):
        with pytest.raises(BackendConfigError, match="must be a table"):
            _parse_role_models({"roles": "claude-opus-4-8"})


class TestParseTierModels:
    def test_sparse_only_declared_tiers(self):
        # Undeclared tiers must stay absent so they can follow whichever
        # provider ultimately wins — see _resolve_tier_models.
        assert _parse_tier_models({"heavy": "x"}) == {"heavy": "x"}

    def test_none_when_nothing_declared(self):
        assert _parse_tier_models({"roles": {"reviewer": "light"}}) is None

    def test_ignores_non_string_tier(self):
        assert _parse_tier_models({"heavy": 7, "light": "y"}) == {"light": "y"}

    def test_overrides_merge_over_provider_defaults(self):
        tiers = _resolve_tier_models("codex", {"heavy": "gpt-custom"})
        assert tiers["heavy"] == "gpt-custom"
        # Undeclared tiers stay on the *codex* defaults, not Claude's.
        assert tiers["light"] == bc._PROVIDER_TIER_MODELS["codex"]["light"]


# --- resolution --------------------------------------------------------------


class TestModelForRole:
    def _cfg(self, roles):
        return BackendConfig(
            tier_models={"heavy": "big", "standard": "mid", "light": "small"},
            role_models=roles,
        )

    def test_unpinned_role_falls_back_to_tier(self):
        assert self._cfg({}).model_for_role("reviewer", "standard") == "mid"

    def test_pin_to_literal_model_id_wins(self):
        cfg = self._cfg({"reviewer": "claude-opus-4-8"})
        assert cfg.model_for_role("reviewer", "standard") == "claude-opus-4-8"

    def test_pin_to_tier_name_resolves_through_tier_table(self):
        # A tier-named pin keeps tracking that tier's [models] override.
        cfg = self._cfg({"mutation_tester": "light"})
        assert cfg.model_for_role("mutation_tester", "standard") == "small"

    def test_pin_only_affects_its_own_role(self):
        cfg = self._cfg({"reviewer": "big"})
        assert cfg.model_for_role("mutation_tester", "standard") == "mid"

    def test_pin_below_floor_is_honored(self):
        # An explicit pin deliberately beats min_model: the floor guards
        # Booley's tier defaults, not a choice the user wrote down.
        cfg = self._cfg({"reviewer": "light"})
        assert cfg.model_for_role("reviewer", "heavy") == "small"


# --- load path ---------------------------------------------------------------


class TestLoadModelsConfig:
    def test_roles_land_in_backend_config(self, tmp_path):
        _write_toml(
            tmp_path,
            '[agent]\nprovider = "claude"\n\n'
            '[models.roles]\nreviewer = "claude-sonnet-4-6"\nmutation_tester = "light"\n',
        )
        load_models_config(tmp_path)
        cfg = bc.get_backend_config()
        assert cfg.model_for_role("reviewer", "heavy") == "claude-sonnet-4-6"
        assert cfg.model_for_role("mutation_tester", "heavy") == cfg.model_for_tier("light")

    def test_developer_pin_reaches_model_map(self, tmp_path):
        _write_toml(tmp_path, '[models.roles]\ndeveloper = "claude-sonnet-4-6"\n')
        load_models_config(tmp_path)
        assert MODEL_MAP["developer"] == "claude-sonnet-4-6"

    def test_models_honored_without_an_agent_table(self, tmp_path):
        # Regression: [models] used to be read only when [agent] existed, so
        # every model setting in an [agent]-less project was silently inert.
        _write_toml(tmp_path, '[models]\nheavy = "custom-heavy"\n')
        load_models_config(tmp_path)
        assert bc.get_backend_config().model_for_tier("heavy") == "custom-heavy"

    def test_tier_override_is_sparse(self, tmp_path):
        _write_toml(tmp_path, '[models]\nheavy = "custom-heavy"\n')
        load_models_config(tmp_path)
        cfg = bc.get_backend_config()
        assert cfg.model_for_tier("light") == bc._PROVIDER_TIER_MODELS["claude"]["light"]

    def test_unknown_role_refuses_to_load(self, tmp_path):
        _write_toml(tmp_path, '[models.roles]\nnope = "claude-opus-4-8"\n')
        with pytest.raises(BackendConfigError, match="unknown role"):
            load_models_config(tmp_path)


class TestSpecialistSubprocessPath:
    """A specialist subprocess never calls load_models_config.

    It resolves through _lazy_backend_config → _project_config_from_env, so the
    project's pins have to survive that path or the knob silently does nothing
    for exactly the agents it was built to configure.
    """

    def test_lazy_config_picks_up_role_pins(self, tmp_path, monkeypatch):
        _write_toml(tmp_path, '[models.roles]\nreviewer = "claude-opus-4-8"\n')
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
        cfg = bc.get_backend_config()
        assert cfg.model_for_role("reviewer", "standard") == "claude-opus-4-8"

    def test_env_provider_handoff_still_applies_project_models(self, tmp_path, monkeypatch):
        # The developer hands off BOOLEY_PRIMARY_PROVIDER, which settles the
        # provider — but the project's pins must still apply.
        _write_toml(
            tmp_path,
            '[models]\nheavy = "custom-heavy"\n\n[models.roles]\nreviewer = "heavy"\n',
        )
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
        cfg = bc.get_backend_config()
        assert cfg.provider == "claude"
        assert cfg.model_for_role("reviewer", "light") == "custom-heavy"

    def test_no_project_dir_leaves_roles_empty(self):
        assert bc.get_backend_config().role_models == {}


class TestSpecialistResolvesRolePin:
    def test_resolve_model_consults_role_pin(self, monkeypatch):
        from booley.specialists.reviewer import ReviewerSpecialist

        cfg = BackendConfig(
            tier_models=dict(bc._DEFAULT_TIER_MODELS),
            role_models={"reviewer": "claude-haiku-4-5"},
        )
        monkeypatch.setattr(bc, "_backend_config", cfg)

        specialist = ReviewerSpecialist()
        specialist._args = SimpleNamespace(model=None)
        assert specialist._resolve_model() == "claude-haiku-4-5"

    def test_unpinned_specialist_uses_floor_tier(self, monkeypatch):
        from booley.specialists.reviewer import ReviewerSpecialist

        cfg = BackendConfig(tier_models=dict(bc._DEFAULT_TIER_MODELS))
        monkeypatch.setattr(bc, "_backend_config", cfg)

        specialist = ReviewerSpecialist()
        specialist._args = SimpleNamespace(model=None)
        assert specialist._resolve_model() == cfg.model_for_tier(specialist.min_model)


# --- vocabulary drift guard --------------------------------------------------


def test_known_roles_matches_specialists_and_steps():
    """_KNOWN_ROLES is hand-maintained; this fails if a specialist is added.

    The config layer can't import the MCP tool registry (bare Specialist
    subprocesses import it at startup), so the role vocabulary is a literal.
    This test is what keeps that literal honest — including for specialists
    currently hidden from discovery via registry.SKIP_MODULES.
    """
    from booley.config.settings import STEP_TIERS
    from booley.mcp.registry import extract_mcp_tool_info

    specialists_dir = Path(bc.__file__).resolve().parent.parent / "specialists"
    specialists = {
        info.name
        for py_file in sorted(specialists_dir.glob("*.py"))
        if (info := extract_mcp_tool_info(py_file, builtin=True)) and info.kind == "specialist"
    }
    assert specialists, "no specialists discovered — the AST probe broke, not the vocabulary"
    assert specialists | set(STEP_TIERS) == _KNOWN_ROLES


class TestDoctorModelsTable:
    """Doctor surfaces a bad [models] before the first ticket dies on it."""

    def _run(self, models):
        from booley.harness import doctor

        passes: list[str] = []
        warns: list[str] = []
        fails: list[str] = []
        valid = doctor._validate_models_table(
            {} if models is None else {"models": models},
            passes.append,
            lambda msg, fix="": warns.append(msg),
            lambda msg, fix="": fails.append(msg),
        )
        return valid, passes, warns, fails

    def test_absent_models_table_is_fine(self):
        valid, passes, warns, fails = self._run(None)
        assert valid and not passes and not warns and not fails

    def test_fails_on_unknown_role(self):
        valid, _passes, _warns, fails = self._run({"roles": {"reviwer": "claude-opus-4-8"}})
        assert not valid
        assert any("reviwer" in m for m in fails)

    def test_fails_when_models_is_not_a_table(self):
        valid, _passes, _warns, fails = self._run("claude-opus-4-8")
        assert not valid
        assert any("[models] must be a table" in m for m in fails)

    def test_warns_on_a_stray_key(self):
        # A misspelled tier is inert, not fatal — warn rather than fail.
        valid, _passes, warns, fails = self._run({"heavyy": "claude-opus-4-8"})
        assert valid and not fails
        assert any("heavyy" in m for m in warns)

    def test_reports_pins_and_tier_overrides(self):
        valid, passes, warns, fails = self._run(
            {"heavy": "custom-heavy", "roles": {"reviewer": "claude-sonnet-4-6"}}
        )
        assert valid and not warns and not fails
        assert any("heavy" in m for m in passes)
        assert any("reviewer=claude-sonnet-4-6" in m for m in passes)

    def test_models_is_a_recognized_top_level_table(self):
        audit = project_schema.audit_known_tables({"models": {}})
        assert audit.findings == ()


def test_tier_names_and_role_names_do_not_collide():
    """model_for_role() reads a pin as a tier name first, so overlap would shadow."""
    assert not (_KNOWN_ROLES & set(_MODEL_TIERS))
