"""A Booley-issued Sandbox can put Codex alone on the gateway (#2897).

Specs are built through the real init path (``_interactive_spec_sources``) against a
disposable host home holding a login for both Agent CLIs, as in ``test_multi_cli_sandbox``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from booley.config import agent as agent_config
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import _claude_backend, auth_token, session_issuance
from booley.runtime import devcontainer as dc

_CLAUDE_CREDS_SEED = f"{dc.AGENT_HOME}/.claude-creds-seed.json"
_CODEX_CREDS_SEED = f"{dc.AGENT_HOME}/.codex-auth-seed.json"
_GATEWAY_URL = "https://gw.example/v1"
_GATEWAY_KEYS = (
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)
_TOML = (
    '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n'
    '[agent.auth]\nclaude = "subscription"\ncodex = "{codex}"\n'
)


def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex: str = "gateway") -> Path:
    """A Project with Codex on *codex* and a host home holding both logins; returns its root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        *_GATEWAY_KEYS,
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
        login.write_bytes(json.dumps(body).encode())
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_bytes(_TOML.format(codex=codex).encode())
    return project


def _issue(project: Path) -> dict:
    """The spec ``booley init`` would write for Claude (built, not sealed)."""
    sources = init_cmd._interactive_spec_sources(
        InitContext(project_root=project), init_cmd._NANGATE_PDK_NOT_REQUESTED, "claude", None
    )
    inputs = session_issuance.SessionSpecInputs(sources.project_data_source, (), (), None, None)
    return sources.build(inputs)


def _targets(spec: dict) -> list[str]:
    return [dc._parse_mount(mount).get("target", "") for mount in spec["mounts"]]


def _hooks(spec: dict) -> str:
    return spec["postCreateCommand"] + "; " + spec["postStartCommand"]


@pytest.mark.parametrize("token_key", ["BOOLEY_GATEWAY_TOKEN", "BOOLEY_GATEWAY_TOKEN_COMMAND"])
def test_issuance_forwards_gateway_refs_and_seeds_no_codex_login(tmp_path, monkeypatch, token_key):
    project = _host(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
    monkeypatch.setenv(token_key, "SYNTHETIC_TOKEN_OR_COMMAND")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")

    spec = _issue(project)

    remote = spec["remoteEnv"]
    assert remote["BOOLEY_AGENT_CLIS"] == "claude=subscription,codex=gateway"
    gateway_remote = {k: v for k, v in remote.items() if k.startswith("BOOLEY_GATEWAY_")}
    assert gateway_remote == {
        "BOOLEY_GATEWAY_URL": "${localEnv:BOOLEY_GATEWAY_URL}",
        token_key: f"${{localEnv:{token_key}}}",
    }
    assert "OPENAI_API_KEY" not in remote
    targets = _targets(spec)
    assert _CLAUDE_CREDS_SEED in targets
    assert _CODEX_CREDS_SEED not in targets
    assert dc._APP_TOKEN_SEED_TARGET["codex"] not in targets
    assert ".codex/auth.json" not in _hooks(spec)
    project_id = dc.canonical_project_id(project)
    volumes = [m for m in spec["mounts"] if dc._parse_mount(m).get("type") == "volume"]
    assert volumes == [
        dc.state_volume_mount("claude", project_id, "subscription"),
        dc.state_volume_mount("codex", project_id, "gateway"),
    ]
    assert "SYNTHETIC" not in json.dumps(spec)  # references only, never a value


def test_issuance_forwards_no_gateway_reference_unless_codex_is_on_the_gateway(
    tmp_path, monkeypatch
):
    project = _host(tmp_path, monkeypatch, codex="subscription")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_TOKEN")

    spec = _issue(project)

    assert spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] == "claude=subscription,codex=subscription"
    assert [k for k in spec["remoteEnv"] if k.startswith("BOOLEY_GATEWAY_")] == []


def test_issuance_without_host_gateway_settings_fails_naming_the_variable(tmp_path, monkeypatch):
    project = _host(tmp_path, monkeypatch)

    with pytest.raises(session_issuance.RuntimeSpecError, match="BOOLEY_GATEWAY_URL"):
        _issue(project)

    # URL set but no token source: the token variables are named instead.
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
    with pytest.raises(session_issuance.RuntimeSpecError, match="BOOLEY_GATEWAY_TOKEN"):
        _issue(project)


def test_gateway_state_volume_belongs_to_the_project():
    project_id = "abc123"
    gateway_volume = dc.state_volume_name("codex", project_id, "gateway")
    assert gateway_volume == f"booley-codex-state-{project_id}-gateway"
    assert gateway_volume in dc.state_volume_names(project_id)


def test_issued_sandbox_routes_codex_through_gateway_and_never_claude(tmp_path, monkeypatch):
    project = _host(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_TOKEN")
    spec = _issue(project)
    toml_data = tomllib.loads((project / ".booley" / "booley.toml").read_text(encoding="utf-8"))

    # Resolve the spec's ``${localEnv:KEY}`` references the way VS Code does, then act as
    # the Sandbox: inside a container, with the default CLI's policy as the hand-off.
    host_env = {"BOOLEY_GATEWAY_URL": _GATEWAY_URL, "BOOLEY_GATEWAY_TOKEN": "SYNTHETIC_TOKEN"}
    for key, value in spec["remoteEnv"].items():
        monkeypatch.setenv(key, host_env.get(key, value) if "localEnv" in value else value)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    assert spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == "subscription"

    settings = agent_config.settings_from_data(toml_data, lazy=True)
    assert settings.auth_for("codex") == "gateway"
    assert settings.auth_for("claude") == "subscription"
    assert agent_config.gateway_handoff_active()  # Codex's own policy, not the default CLI's
    overrides = _claude_backend._claude_env_overrides("subscription")
    assert overrides["BOOLEY_GATEWAY_URL"] == ""
    assert overrides["BOOLEY_GATEWAY_TOKEN"] == ""
    assert overrides["BOOLEY_HIDDEN_GATEWAY"] == "1"
