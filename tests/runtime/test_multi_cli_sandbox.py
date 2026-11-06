"""A Booley-issued Sandbox seeds exactly the Agent CLIs ``[agent] clis`` allows.

Specs are built through the real init path (``_interactive_spec_sources``) against a
disposable host home holding a login for both Agent CLIs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import auth_token, session_issuance
from booley.runtime import devcontainer as dc

_CLAUDE_CREDS_SEED = f"{dc.AGENT_HOME}/.claude-creds-seed.json"
_CODEX_CREDS_SEED = f"{dc.AGENT_HOME}/.codex-auth-seed.json"


def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str) -> Path:
    """A Project with *toml* and a host home with Claude and Codex logins; returns its root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_AGENT_APP",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    logins = {
        "claude": {"claudeAiOauth": {"accessToken": "SYNTHETIC_CLAUDE_LOGIN"}},
        "codex": {"tokens": {"access_token": "SYNTHETIC_CODEX_LOGIN"}, "OPENAI_API_KEY": None},
    }
    for app, body in logins.items():
        login = auth_token.subscription_creds_path(app)
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text(json.dumps(body))
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_text(toml)
    return project


def _issue(project: Path, app: str) -> dict:
    """The spec ``booley init`` would write for *app* (built, not sealed)."""
    sources = init_cmd._interactive_spec_sources(
        InitContext(project_root=project), init_cmd._NANGATE_PDK_NOT_REQUESTED, app, None
    )
    inputs = session_issuance.SessionSpecInputs(sources.project_data_source, (), (), None, None)
    return sources.build(inputs)


def _targets(spec: dict) -> list[str]:
    return [dc._parse_mount(mount).get("target", "") for mount in spec["mounts"]]


def _volumes(spec: dict) -> list[str]:
    return [m for m in spec["mounts"] if dc._parse_mount(m).get("type") == "volume"]


def _hooks(spec: dict) -> str:
    return spec["postCreateCommand"] + "; " + spec["postStartCommand"]


def test_issued_spec_seeds_every_listed_cli(tmp_path, monkeypatch):
    project = _host(
        tmp_path, monkeypatch, '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")
    auth_token.store_token("SYNTHETIC_STORED_CODEX", "codex")

    spec = _issue(project, "claude")

    assert spec["name"] == "Booley Interactive (claude, codex)"
    assert spec["remoteEnv"]["BOOLEY_AGENT_APP"] == "claude"
    assert spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] == "claude=auto,codex=auto"
    targets = _targets(spec)
    for seed in (_CLAUDE_CREDS_SEED, _CODEX_CREDS_SEED, dc._APP_TOKEN_SEED_TARGET["codex"]):
        assert seed in targets
    project_id = dc.canonical_project_id(project)
    assert _volumes(spec) == [
        dc.state_volume_mount("claude", project_id),
        dc.state_volume_mount("codex", project_id),
    ]
    assert ".codex/auth.json" in _hooks(spec)
    assert ".claude/.credentials.json" in _hooks(spec)
    assert spec["remoteEnv"]["OPENAI_API_KEY"] == "${localEnv:OPENAI_API_KEY}"


def test_single_cli_spec_seeds_only_claude_as_before(tmp_path, monkeypatch):
    project = _host(tmp_path, monkeypatch, '[agent]\nclis = ["claude"]\n')
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")

    spec = _issue(project, "claude")

    assert spec["name"] == "Booley Interactive (claude)"
    assert spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] == "claude=auto"
    assert spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == "auto"
    assert "OPENAI_API_KEY" not in spec["remoteEnv"]
    assert _CODEX_CREDS_SEED not in _targets(spec)
    assert _CLAUDE_CREDS_SEED in _targets(spec)
    assert _volumes(spec) == [dc.state_volume_mount("claude", dc.canonical_project_id(project))]
    assert ".codex" not in _hooks(spec)
    # Exact hooks as before: the Claude login copy, then the fixed runtime tail.
    assert spec["postStartCommand"].split("; ")[0] in session_issuance._FIXED_SEED_FRAGMENTS
    assert len(spec["postStartCommand"].split("; ")) == 2


def test_each_cli_seeds_per_its_own_auth_policy(tmp_path, monkeypatch):
    project = _host(
        tmp_path,
        monkeypatch,
        '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n'
        '[agent.auth]\nclaude = "subscription"\ncodex = "api_key"\n',
    )
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_ANTHROPIC")

    spec = _issue(project, "claude")

    remote = spec["remoteEnv"]
    assert remote["BOOLEY_AGENT_CLIS"] == "claude=subscription,codex=api_key"
    assert remote["BOOLEY_PRIMARY_AUTH"] == "subscription"
    targets = _targets(spec)
    assert _CLAUDE_CREDS_SEED in targets  # subscription copies the login
    assert _CODEX_CREDS_SEED not in targets  # api_key copies none
    assert remote["OPENAI_API_KEY"] == "${localEnv:OPENAI_API_KEY}"
    assert "ANTHROPIC_API_KEY" not in remote  # subscription never forwards the key
    project_id = dc.canonical_project_id(project)
    assert _volumes(spec) == [
        dc.state_volume_mount("claude", project_id, "subscription"),
        dc.state_volume_mount("codex", project_id, "api_key"),
    ]


def test_listed_claude_with_ambiguous_config_fails_naming_claude(tmp_path, monkeypatch):
    project = _host(
        tmp_path,
        monkeypatch,
        '[agent]\nclis = ["codex", "claude"]\ndefault = "codex"\n\n'
        '[agent.auth]\nclaude = "subscription"\n',
    )
    (Path.home() / ".claude.json").write_text(
        json.dumps({"customApiKeyResponses": {"approved": ["SYNTHETIC"]}})
    )

    with pytest.raises(session_issuance.RuntimeSpecError, match="claude"):
        _issue(project, "codex")
