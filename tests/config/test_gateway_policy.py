"""Gateway auth policy (#2266): validation and the BOOLEY_PRIMARY_AUTH hand-off."""

from __future__ import annotations

import pytest

from booley.config import agent as agent_config
from booley.config import gateway

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"


@pytest.fixture
def gateway_container(monkeypatch):
    """A Sandbox-like process: gateway hand-off honored, no ambient auth state."""
    for name in (
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_GATEWAY_URL",
        "BOOLEY_GATEWAY_TOKEN",
        "BOOLEY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    agent_config.set_agent_settings(None)
    yield monkeypatch
    agent_config.set_agent_settings(None)


def test_gateway_policy_accepts_complete_https_config():
    settings = gateway.gateway_settings_from_env(
        {"BOOLEY_GATEWAY_URL": f" {_URL} ", "BOOLEY_GATEWAY_TOKEN": f" {_TOKEN}\n"}
    )
    assert settings == gateway.GatewaySettings(url=_URL, token=_TOKEN)
    assert _TOKEN not in repr(settings)


def test_gateway_policy_rejects_missing_url():
    with pytest.raises(gateway.GatewayConfigError, match="needs BOOLEY_GATEWAY_URL"):
        gateway.gateway_settings_from_env({"BOOLEY_GATEWAY_TOKEN": _TOKEN})


def test_gateway_policy_rejects_http_url():
    with pytest.raises(gateway.GatewayConfigError, match="BOOLEY_GATEWAY_URL must be an https://"):
        gateway.gateway_settings_from_env(
            {"BOOLEY_GATEWAY_URL": "http://gateway.example/v1", "BOOLEY_GATEWAY_TOKEN": _TOKEN}
        )


def test_gateway_policy_rejects_missing_token():
    with pytest.raises(gateway.GatewayConfigError, match="needs BOOLEY_GATEWAY_TOKEN"):
        gateway.gateway_settings_from_env(
            {"BOOLEY_GATEWAY_URL": _URL, "BOOLEY_GATEWAY_TOKEN": "  "}
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:SYNTHETIC_PASSWORD@gateway.example/v1",
        "https:///v1",
        "https://gateway.example/v1 extra",
        "https://gateway.example/v1?api_key=SYNTHETIC_QUERY",
    ],
)
def test_gateway_policy_rejects_unsafe_url_without_echoing_it(url):
    with pytest.raises(gateway.GatewayConfigError) as caught:
        gateway.gateway_settings_from_env(
            {"BOOLEY_GATEWAY_URL": url, "BOOLEY_GATEWAY_TOKEN": _TOKEN}
        )
    assert "SYNTHETIC" not in str(caught.value)


def test_gateway_handoff_reaches_loaded_agent_settings(gateway_container, tmp_path):
    """The MCP server's toml-only loader must see the env-only policy."""
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_text(
        '[agent]\nprovider = "codex"\nauth = "auto"\n', encoding="utf-8"
    )
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "gateway")

    settings = agent_config.load_agent_settings(project)

    assert settings.auth == "gateway"
    assert agent_config.resolve_auth_policy() == "gateway"


def test_gateway_handoff_with_surrounding_whitespace_is_one_policy_everywhere(gateway_container):
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "gateway \n")
    gateway_container.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")

    assert agent_config.resolve_auth_policy() == "gateway"
    assert agent_config.get_agent_settings().auth == "gateway"
