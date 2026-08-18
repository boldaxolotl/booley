"""Tests for init Step 3 agent-authentication reporting (``_step_auth``).

The step probes each supported provider and reports which have usable auth. The
guarantee under test: when *any* provider resolves, a missing alternate is
demoted to an informational line — never a bare ``[!!]`` warning that reads as a
setup failure (SETUP-REPORT F1). Only a *total* absence of auth warns.
"""

from __future__ import annotations

import pytest

from booley.config.agent import _DEFAULT_PROVIDER
from booley.harness import init_cmd
from booley.harness.init_common import InitContext


@pytest.fixture
def captured(monkeypatch):
    """Capture ok/info/warn lines emitted by ``_step_auth`` by level."""
    sink: dict[str, list[str]] = {"ok": [], "info": [], "warn": []}
    monkeypatch.setattr(init_cmd, "banner", lambda *a, **k: None)
    monkeypatch.setattr(init_cmd, "ok", sink["ok"].append)
    monkeypatch.setattr(init_cmd, "info", sink["info"].append)
    monkeypatch.setattr(init_cmd, "warn", sink["warn"].append)
    # _check_provider_creds emits secondary ok/warn lines of its own; silence it
    # so assertions target only _step_auth's own reporting.
    monkeypatch.setattr(init_cmd, "_check_provider_creds", lambda *a, **k: None)
    return sink


def _fake_modes(monkeypatch, modes: dict[str, str | None]) -> None:
    monkeypatch.setattr(init_cmd, "_detect_auth_mode", modes.get)


def test_missing_alternate_is_info_not_warn_when_default_resolves(captured, monkeypatch):
    # Claude present, codex absent → codex's "not detected" must be info, not warn.
    _fake_modes(monkeypatch, {"claude": "subscription", "codex": None})
    ctx = InitContext()
    init_cmd._step_auth(ctx)

    assert captured["warn"] == []  # nothing scary when a provider resolved
    assert any("codex auth not detected" in m for m in captured["info"])
    assert any("auth available: claude/subscription" in m for m in captured["ok"])
    assert ctx.results[-1].status == "ok"


def test_default_provider_reported_first(captured, monkeypatch):
    # Both resolve → the default provider's detect line leads the output.
    _fake_modes(monkeypatch, {"claude": "subscription", "codex": "api_key"})
    init_cmd._step_auth(ctx := InitContext())
    detects = [m for m in captured["ok"] if m.startswith("detected ")]
    assert detects[0].endswith(_DEFAULT_PROVIDER)
    assert ctx.results[-1].status == "ok"


def test_no_provider_resolves_warns(captured, monkeypatch):
    # Total absence of auth is the one case that legitimately warns.
    _fake_modes(monkeypatch, {"claude": None, "codex": None})
    init_cmd._step_auth(ctx := InitContext())
    assert any("no agent auth detected" in m for m in captured["warn"])
    assert ctx.results[-1].status == "warn"


def test_info_banner_names_the_real_default(captured, monkeypatch):
    # The intro line must name the actual default provider, not a stale literal.
    _fake_modes(monkeypatch, {"claude": "subscription", "codex": None})
    init_cmd._step_auth(InitContext())
    assert any(f"defaults to {_DEFAULT_PROVIDER}" in m for m in captured["info"])


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
