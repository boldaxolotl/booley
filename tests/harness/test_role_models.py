"""Tests for the Agent CLI set (``[agent]``) and per-Specialist model pins (``[models]``).

Covers parsing/validation, resolution in AgentSettings, the eager loader and the
Specialist subprocess (lazy) path, Sandbox credential policy, and Doctor's audits.
Symbols added with the per-Specialist model table are imported inside test bodies so
these tests fail in their bodies, not at collection, against older code.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from booley.audit import project_schema
from booley.audit.config_common import ConfigFindingSeverity
from booley.config import agent as bc
from booley.config.agent import AgentSettings, BackendConfigError, load_agent_settings

# Booley's built-in models per Agent CLI: (Specialist model, internal model, Specialist effort).
_BUILTIN = {
    "claude": ("claude-opus-5-5", "claude-sonnet-5-5", None),
    "codex": ("gpt-6.1-sol", "gpt-6-luna", "high"),
}


# Literal (not bc.SPECIALISTS) so collection works on any version; the drift guard
# below keeps the Specialist vocabulary itself honest.
_SPECIALIST_NAMES = ("coverage_analyst", "mutation_tester", "reviewer")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate from ambient harness hand-offs and any installed global config."""
    for var in (
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_DIR",
        "BOOLEY_AGENT_APP",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(bc, "_agent_settings", None)
    monkeypatch.setattr(bc, "_inside_container", lambda: False)
    yield


def _write_toml(root: Path, body: str) -> Path:
    """Create a project whose .booley/booley.toml holds *body*."""
    project_data = root / ".booley"
    project_data.mkdir(parents=True, exist_ok=True)
    (project_data / "booley.toml").write_text(body)
    return root


# --- load path ---------------------------------------------------------------


_PROVIDER_AND_STRING_AUTH_RESOLVE_AS_TODAY_CASES = ["claude", "codex"]


def test_provider_and_string_auth_resolve_as_today(tmp_path):
    """A pre-existing ``provider`` + string ``auth`` config keeps today's models."""
    for cli in _PROVIDER_AND_STRING_AUTH_RESOLVE_AS_TODAY_CASES:
        _write_toml(tmp_path, f'[agent]\nprovider = "{cli}"\nauth = "subscription"\n')
        settings = load_agent_settings(tmp_path)
        specialist_model, internal_model, effort = _BUILTIN[cli]

        assert settings.default_cli == cli
        assert settings.auth_for(cli) == "subscription"
        for name in sorted(bc.SPECIALISTS):
            assert settings.cli_for_specialist(name) == cli
            assert settings.model_for_specialist(name) == specialist_model
        assert settings.internal_model() == internal_model
        assert settings.effort_for(cli) == effort


_INVALID_CONFIG_FAILS_LOADING_NAMING_KEY_CASES = [
    ('[models]\nreviwer = "claude-opus-5-5"\n', ("reviwer",)),
    ('[models]\nreviewer = "o3-mini"\n', ("reviewer",)),
    ('[agent]\ndefault = "claude"\n\n[models]\nreviewer = "gpt-6-luna"\n', ("reviewer", "codex")),
    ('[agent]\ndefault = "claude"\nprovider = "codex"\n', ("default", "provider")),
    ('[agent.auth]\nclaude = "gateway"\n', ("[agent.auth] claude",)),
    ('[agent]\nclis = ["claude", "codex"]\nauth = "gateway"\n', ("[agent] auth", "claude")),
]


def test_invalid_config_fails_loading_naming_key(tmp_path):
    """Loading and Doctor's audit refuse the same config with the same message."""
    for body, keys in _INVALID_CONFIG_FAILS_LOADING_NAMING_KEY_CASES:
        from booley.audit.agent_schema import audit_agent_table, audit_models_table

        _write_toml(tmp_path, body)
        with pytest.raises(BackendConfigError) as excinfo:
            load_agent_settings(tmp_path)
        message = str(excinfo.value)
        for key in keys:
            assert key in message

        data = tomllib.loads(body)
        findings = audit_agent_table(data).findings + audit_models_table(data).findings
        fails = [f.message for f in findings if f.severity is ConfigFindingSeverity.FAIL]
        assert fails == [message]


_RETIRED_MODEL_KEYS_FAIL_WITH_MIGRATION_ENTRIES_CASES = [
    (
        '[models]\nstandard = "claude-sonnet-5-5"\n',
        ["[models]", *(f'{n} = "claude-sonnet-5-5"' for n in _SPECIALIST_NAMES)],
    ),
    (
        '[agent]\ndefault = "claude"\n\n[models.roles]\nreviewer = "gpt-6-luna"\n',
        [
            "[models]",
            'reviewer = "gpt-6-luna"',
            'mutation_tester = "claude-opus-5-5"',
            "[agent]",
            'clis = ["claude", "codex"]',
        ],
    ),
]


def test_retired_model_keys_fail_with_migration_entries(tmp_path):
    """Tier keys and ``[models.roles]`` fail, showing the equivalent new lines."""
    for body, expected in _RETIRED_MODEL_KEYS_FAIL_WITH_MIGRATION_ENTRIES_CASES:
        _write_toml(tmp_path, body)
        with pytest.raises(BackendConfigError) as excinfo:
            load_agent_settings(tmp_path)
        lines = str(excinfo.value).splitlines()
        for line in expected:
            assert line in lines


_EAGER_AND_LAZY_RESOLVE_THE_SAME_DEFAULT_CLI_CASES = [
    (
        {"BOOLEY_PRIMARY_PROVIDER": "codex"},
        '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n',
        "codex",
    ),
    ({"BOOLEY_AGENT_APP": "codex"}, "", "codex"),
    ({"BOOLEY_AGENT_APP": "none"}, "", "claude"),
    ({}, '[agent]\ndefault = "codex"\n', "codex"),
    ({}, '[agent]\nclis = ["codex", "claude"]\n', "codex"),
]


def test_eager_and_lazy_resolve_the_same_default_cli(tmp_path, monkeypatch):
    """The MCP server's eager loader and a Specialist's lazy loader agree on the CLI."""
    for env, body, expected in _EAGER_AND_LAZY_RESOLVE_THE_SAME_DEFAULT_CLI_CASES:
        for name in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_AGENT_APP", "BOOLEY_DIR"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        _write_toml(tmp_path, body)

        eager = load_agent_settings(tmp_path).default_cli
        monkeypatch.setattr(bc, "_agent_settings", None)
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
        lazy = bc.get_agent_settings().default_cli

        assert (eager, lazy) == (expected, expected)


@pytest.mark.parametrize(
    ("model_pin", "expected"),
    [
        pytest.param(
            None,
            {
                "claude-opus-5-5": ("[models] coverage_analyst", "[models] mutation_tester"),
                "gpt-6-luna": ("[models] reviewer",),
            },
            id="pins",
        ),
        pytest.param(
            "gw-model",
            {
                "claude-opus-5-5": ("[models] coverage_analyst", "[models] mutation_tester"),
                "gw-model": ("BOOLEY_GATEWAY_MODEL",),
            },
            id="gateway-pin-codex-only",
        ),
    ],
)
def test_model_settings_maps_each_model_to_its_keys(model_pin, expected):
    """Every Specialist model maps to the key selecting it; the gateway pin hits Codex only."""
    settings = AgentSettings(
        clis=("claude", "codex"),
        specialist_models={"reviewer": "gpt-6-luna"},
        model_pin=model_pin,
    )
    assert settings.model_settings() == expected


def test_project_credential_policy_refuses_toml_gateway(tmp_path):
    """Sandbox Issuance cannot route Codex through the gateway from booley.toml yet."""
    _write_toml(tmp_path, '[agent]\nprovider = "codex"\nauth = "gateway"\n')
    with pytest.raises(BackendConfigError, match="gateway"):
        bc.project_credential_policy(tmp_path, booley_dir=tmp_path / ".booley")

    _write_toml(
        tmp_path,
        '[agent]\nclis = ["claude", "codex"]\ndefault = "codex"\n\n'
        '[agent.auth]\nclaude = "api_key"\ncodex = "subscription"\n',
    )
    assert bc.project_credential_policy(tmp_path, booley_dir=tmp_path / ".booley") == (
        "codex",
        "subscription",
    )


def test_malformed_clis_entries_fail_naming_the_key(tmp_path):
    """A nested list or table in ``clis`` is a config error, not an unhashable crash."""
    for entry in ('["claude"]', "{ a = 1 }", '"claude", "claude"', '"gemini"'):
        _write_toml(tmp_path, f"[agent]\nclis = [{entry}]\n")
        with pytest.raises(BackendConfigError, match="clis"):
            load_agent_settings(tmp_path)


def test_project_credential_policy_refuses_gateway_for_any_listed_cli(tmp_path):
    """Sandbox Issuance refuses a toml ``gateway`` even when Codex is not the default CLI."""
    _write_toml(
        tmp_path,
        '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n[agent.auth]\ncodex = "gateway"\n',
    )
    with pytest.raises(BackendConfigError, match="gateway"):
        bc.project_credential_policy(tmp_path, booley_dir=tmp_path / ".booley")


def test_models_audit_reports_an_environment_error_without_an_agent_table(monkeypatch):
    """With no ``[agent]`` table to report it, the ``[models]`` audit reports the failure."""
    from booley.audit.agent_schema import audit_models_table

    def _boom(_declared):
        raise BackendConfigError("environment boom")

    monkeypatch.setattr(bc, "resolve_default_cli", _boom)
    audit = audit_models_table({"models": {"reviewer": "claude-opus-5-5"}})

    assert [f.message for f in audit.findings] == ["environment boom"]


# --- vocabulary drift guard --------------------------------------------------


def test_specialists_match_registered_specialist_names():
    """SPECIALISTS is hand-maintained; this fails if a Specialist is added or renamed.

    The config layer can't import the MCP tool registry (bare Specialist
    subprocesses import it at startup), so the vocabulary is a literal. This
    test keeps it honest, including for Specialists hidden via registry.SKIP_MODULES.
    """
    from booley.mcp.registry import extract_mcp_tool_info

    specialists_dir = Path(bc.__file__).resolve().parent.parent / "specialists"
    specialists = {
        info.name
        for py_file in sorted(specialists_dir.glob("*.py"))
        if (info := extract_mcp_tool_info(py_file, builtin=True)) and info.kind == "specialist"
    }
    assert specialists, "no specialists discovered — the AST probe broke, not the vocabulary"
    assert specialists == bc.SPECIALISTS


# --- Doctor ------------------------------------------------------------------


def test_models_is_a_recognized_top_level_table():
    audit = project_schema.audit_known_tables({"models": {}})
    assert audit.findings == ()


# --- hidden [models] fallback list (#2836, ADR 0084 amendment) -------------------

_MODEL_LIST_SHAPE_ERRORS_FAIL_LOADING_NAMING_KEY_CASES = [
    pytest.param("reviewer = []", "[models] reviewer is an empty list", id="empty"),
    pytest.param(
        "reviewer = [1]", "[models] reviewer entry 1 must be a model ID string", id="integer"
    ),
    pytest.param(
        'reviewer = [["gpt-6-luna"]]',
        "[models] reviewer entry 1 must be a model ID string",
        id="nested-list",
    ),
    pytest.param(
        'reviewer = ["claude-opus-5-5", ""]',
        "[models] reviewer entry 2 must be a model ID string",
        id="blank",
    ),
    pytest.param(
        'reviewer = ["llama-3"]',
        "[models] reviewer entry 'llama-3' names no known Agent CLI",
        id="unknown-prefix",
    ),
]


@pytest.mark.parametrize(
    ("line", "fragment"), _MODEL_LIST_SHAPE_ERRORS_FAIL_LOADING_NAMING_KEY_CASES
)
def test_model_list_shape_errors_fail_loading_naming_key(tmp_path, line, fragment):
    """An empty list or a bad entry fails loading and names the ``[models]`` key."""
    _write_toml(tmp_path, f"[models]\n{line}\n")

    with pytest.raises(BackendConfigError) as excinfo:
        load_agent_settings(tmp_path)

    assert fragment in str(excinfo.value)


def test_model_list_entry_outside_clis_loads(tmp_path):
    """A list entry whose CLI is not in ``[agent] clis`` loads; a string pin still fails."""
    _write_toml(
        tmp_path,
        '[agent]\ndefault = "claude"\n\n[models]\nreviewer = ["claude-opus-5-5", "gpt-6-luna"]\n',
    )

    settings = load_agent_settings(tmp_path)

    assert settings.clis == ("claude",)
    assert settings.model_candidates("reviewer") == ("claude-opus-5-5", "gpt-6-luna")
    assert settings.model_for_specialist("reviewer") == "claude-opus-5-5"
    assert settings.cli_for_specialist("reviewer") == "claude"
    assert settings.model_candidates("mutation_tester") is None
    assert settings.model_settings()["gpt-6-luna"] == ("[models] reviewer",)

    _write_toml(tmp_path, '[agent]\ndefault = "claude"\n\n[models]\nreviewer = "gpt-6-luna"\n')
    with pytest.raises(BackendConfigError, match="not in \\[agent\\] clis"):
        load_agent_settings(tmp_path)


def test_model_list_gateway_pin_replaces_codex_entries(tmp_path):
    """The gateway fixes the model server-side: Codex entries collapse to the pin."""
    settings = AgentSettings(
        default_cli="claude",
        clis=("claude", "codex"),
        specialist_models={"reviewer": ("gpt-6-luna", "gpt-6-astra", "claude-opus-5-5")},
        model_pin="gpt-6-luna",
    )

    assert settings.model_candidates("reviewer") == ("gpt-6-luna", "claude-opus-5-5")
