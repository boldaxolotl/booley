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


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_provider_and_string_auth_resolve_as_today(tmp_path, cli):
    """A pre-existing ``provider`` + string ``auth`` config keeps today's models."""
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


@pytest.mark.parametrize(
    ("body", "keys"),
    [
        pytest.param(
            '[models]\nreviwer = "claude-opus-5-5"\n', ("reviwer",), id="unknown-specialist"
        ),
        pytest.param('[models]\nreviewer = "o3-mini"\n', ("reviewer",), id="unknown-model-prefix"),
        pytest.param(
            '[agent]\ndefault = "claude"\n\n[models]\nreviewer = "gpt-6-luna"\n',
            ("reviewer", "codex"),
            id="cli-not-allowed",
        ),
        pytest.param(
            '[agent]\ndefault = "claude"\nprovider = "codex"\n',
            ("default", "provider"),
            id="provider-default-conflict",
        ),
        pytest.param(
            '[agent.auth]\nclaude = "gateway"\n', ("[agent.auth] claude",), id="gateway-for-claude"
        ),
        pytest.param(
            '[agent]\nclis = ["claude", "codex"]\nauth = "gateway"\n',
            ("[agent] auth", "claude"),
            id="gateway-string-with-claude",
        ),
    ],
)
def test_invalid_config_fails_loading_naming_key(tmp_path, body, keys):
    """Loading and Doctor's audit refuse the same config with the same message."""
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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            '[models]\nstandard = "claude-sonnet-5-5"\n',
            ["[models]", *(f'{n} = "claude-sonnet-5-5"' for n in _SPECIALIST_NAMES)],
            id="tier-override",
        ),
        pytest.param(
            '[agent]\ndefault = "claude"\n\n[models.roles]\nreviewer = "gpt-6-luna"\n',
            [
                "[models]",
                'reviewer = "gpt-6-luna"',
                'mutation_tester = "claude-opus-5-5"',
                "[agent]",
                'clis = ["claude", "codex"]',
            ],
            id="roles-pin",
        ),
    ],
)
def test_retired_model_keys_fail_with_migration_entries(tmp_path, body, expected):
    """Tier keys and ``[models.roles]`` fail, showing the equivalent new lines."""
    _write_toml(tmp_path, body)
    with pytest.raises(BackendConfigError) as excinfo:
        load_agent_settings(tmp_path)
    lines = str(excinfo.value).splitlines()
    for line in expected:
        assert line in lines


@pytest.mark.parametrize(
    ("env", "body", "expected"),
    [
        pytest.param(
            {"BOOLEY_PRIMARY_PROVIDER": "codex"},
            '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n',
            "codex",
            id="env-over-toml",
        ),
        pytest.param({"BOOLEY_AGENT_APP": "codex"}, "", "codex", id="explicit-app"),
        pytest.param({"BOOLEY_AGENT_APP": "none"}, "", "claude", id="app-none-skipped"),
        pytest.param({}, '[agent]\ndefault = "codex"\n', "codex", id="toml-default"),
        pytest.param({}, '[agent]\nclis = ["codex", "claude"]\n', "codex", id="clis-first-entry"),
    ],
)
def test_eager_and_lazy_resolve_the_same_default_cli(tmp_path, monkeypatch, env, body, expected):
    """The MCP server's eager loader and a Specialist's lazy loader agree on the CLI."""
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
