"""Tests for init's configured-provider authentication reporting."""

from __future__ import annotations

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext


@pytest.fixture
def captured(monkeypatch):
    """Capture ok/info/warn lines emitted by ``_step_auth`` by level."""
    sink: dict[str, list[str]] = {"ok": [], "info": [], "skip": [], "warn": []}
    monkeypatch.setattr(init_cmd, "banner", lambda *a, **k: None)
    monkeypatch.setattr(init_cmd, "ok", sink["ok"].append)
    monkeypatch.setattr(init_cmd, "info", sink["info"].append)
    monkeypatch.setattr(init_cmd, "skip", sink["skip"].append)
    monkeypatch.setattr(init_cmd, "warn", sink["warn"].append)
    # _check_provider_creds emits secondary ok/warn lines of its own; silence it
    # so assertions target only _step_auth's own reporting.
    monkeypatch.setattr(init_cmd, "_check_provider_creds", lambda *a, **k: None)
    return sink


def _fake_modes(monkeypatch, modes: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        init_cmd, "_detect_auth_mode", lambda provider, _policy: modes.get(provider)
    )


def test_reports_only_the_configured_provider(captured, monkeypatch):
    _fake_modes(monkeypatch, {"claude": "subscription", "codex": None})
    ctx = InitContext()
    init_cmd._step_auth(ctx, init_cmd.AgentSelection("claude", "subscription"))

    assert captured["warn"] == []
    assert not any("codex" in message for messages in captured.values() for message in messages)
    assert any("detected subscription auth for claude" in m for m in captured["ok"])
    assert ctx.results[-1].status == "ok"


def test_selected_policy_is_used_for_detection(captured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        init_cmd,
        "_detect_auth_mode",
        lambda provider, policy: calls.append((provider, policy)) or "api_key",
    )
    init_cmd._step_auth(ctx := InitContext(), init_cmd.AgentSelection("codex", "api_key"))
    assert calls == [("codex", "api_key")]
    assert ctx.results[-1].status == "ok"


def test_selected_provider_without_auth_warns(captured, monkeypatch):
    _fake_modes(monkeypatch, {"claude": None, "codex": None})
    init_cmd._step_auth(ctx := InitContext(), init_cmd.AgentSelection("codex", "subscription"))
    assert any("no subscription auth available for codex" in m for m in captured["warn"])
    assert ctx.results[-1].status == "warn"


def test_info_banner_names_explicit_selection(captured, monkeypatch):
    _fake_modes(monkeypatch, {"claude": "subscription", "codex": None})
    init_cmd._step_auth(InitContext(), init_cmd.AgentSelection("claude", "subscription"))
    assert "configured agent: claude/subscription" in captured["info"]


def test_skip_credentials_does_not_inspect_or_warn(captured, monkeypatch):
    monkeypatch.setattr(
        init_cmd,
        "_detect_auth_mode",
        lambda *_args: pytest.fail("credential detection must be skipped"),
    )

    ctx = InitContext()
    init_cmd._step_auth(
        ctx,
        init_cmd.AgentSelection("codex", "subscription"),
        skip_credentials=True,
    )

    assert captured["warn"] == []
    assert captured["skip"] == ["credential check skipped by --skip-credentials"]
    assert ctx.results[-1].status == "skip"
    assert ctx.results[-1].detail == "credential check skipped"


def test_detect_auth_mode_reports_what_actually_bills(tmp_path, monkeypatch):
    # The historical misreport: a subscription login plus an exported
    # ANTHROPIC_API_KEY was reported as "subscription", but the key is what the
    # agent CLI uses (and bills). Detection must follow the CLI's precedence.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    assert init_cmd._detect_auth_mode("claude") == "api_key"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert init_cmd._detect_auth_mode("claude") == "subscription"
