"""Review follow-ups for ``BOOLEY_AGENT_APP=auto``: restarts, status, leaks, shell.

Reuses the isolated in-Sandbox fixture and helpers of ``test_agent_app_auto``.
"""

from __future__ import annotations

import argparse

import pytest
from tests.runtime import test_agent_app_auto as base
from tests.runtime.test_agent_app_auto import (
    _codex_config,
    _give,
    _read,
    _refusal,
    _restart_with,
    _start,
)

from booley.config import agent as config_agent
from booley.config.agent import get_agent_settings
from booley.harness import auth_cmd
from booley.harness import booley as booley_cli

sandbox = base.sandbox  # the shared isolated in-Sandbox start fixture


def test_a_vanished_codex_key_is_not_mistaken_for_a_login(sandbox, monkeypatch, capsys):
    """The registrar's own auth.json copy of a gone key must not win the next start."""
    _give("codex-api-key", sandbox.home, monkeypatch)
    _start(capsys)
    assert "synthetic-codex-key" in _read(sandbox.home / ".codex" / "auth.json")

    monkeypatch.delenv("OPENAI_API_KEY")
    _give("gateway", sandbox.home, monkeypatch)
    status = _start(capsys)

    assert "app:codex" in status
    assert "no Claude or Codex credential" in status
    assert 'model_provider = "booley_gateway"' in _codex_config(sandbox.home)


def test_a_vanished_codex_key_with_nothing_else_resolves_to_claude(sandbox, monkeypatch, capsys):
    _give("codex-api-key", sandbox.home, monkeypatch)
    _start(capsys)

    monkeypatch.delenv("OPENAI_API_KEY")
    status = _start(capsys)

    assert "app:claude" in status


def test_a_login_file_with_more_than_the_key_still_counts(sandbox, monkeypatch, capsys):
    _give("codex-login", sandbox.home, monkeypatch)

    assert "Codex credential from" in _start(capsys)


@pytest.mark.parametrize("kind", ["codex-api-key", "gateway"])
def test_auth_status_follows_the_start_choice_without_a_pin(sandbox, monkeypatch, capsys, kind):
    _give(kind, sandbox.home, monkeypatch)
    _start(capsys)
    capsys.readouterr()

    code = auth_cmd.run_auth(argparse.Namespace(status=True, app=None), sandbox.project)

    output = capsys.readouterr()
    text = output.out + output.err
    assert code == 0
    assert "claude" not in text


def test_no_credential_value_reaches_the_record_or_the_status_line(sandbox, monkeypatch, capsys):
    for kind in ("both", "gateway", "claude-oauth-token"):
        _give(kind, sandbox.home, monkeypatch)
    status = _start(capsys)
    record = _read(sandbox.home / ".booley-agent-app.json")

    for secret in (
        "synthetic-claude-key",
        "synthetic-claude-token",
        "synthetic-codex-key",
        "synthetic-gateway-token",
    ):
        assert secret not in status + record


def test_a_claude_credential_beats_the_gateway_policy_hand_off(sandbox, monkeypatch, capsys):
    _give("gateway", sandbox.home, monkeypatch)
    _give("claude-api-key", sandbox.home, monkeypatch)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")

    status = _start(capsys)

    assert "app:claude" in status
    settings = get_agent_settings()
    assert (settings.provider, settings.auth) == ("claude", "auto")
    assert "booley_gateway" not in _codex_config(sandbox.home)


def test_a_record_missing_a_field_is_refused_not_a_traceback(sandbox):
    (sandbox.home / ".booley-agent-app.json").write_text('{"schema": 1, "app": "claude"}')

    assert "incontainer_register" in _refusal(get_agent_settings)


def test_a_gateway_that_stopped_being_configured_still_leaves_codex_working(
    sandbox, monkeypatch, capsys
):
    """Codex routed through a gateway earlier, then the gateway variables vanish."""
    _give("gateway", sandbox.home, monkeypatch)
    _start(capsys)
    for name in ("BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_TOKEN", "BOOLEY_GATEWAY_MODEL"):
        monkeypatch.delenv(name)

    _restart_with("codex-api-key", sandbox, monkeypatch, capsys)

    codex = _codex_config(sandbox.home)
    assert "booley_gateway" not in codex
    assert "model_provider" not in codex


def test_shell_refuses_auto_on_the_host_instead_of_a_traceback(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(config_agent, "_inside_container", lambda: False)

    code = booley_cli._cmd_shell(argparse.Namespace(net=False, command=[]), sandbox.project)

    assert (code, "BOOLEY_AGENT_APP" in capsys.readouterr().err) == (2, True)
