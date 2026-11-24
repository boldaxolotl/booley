"""Tests for init's configured-provider authentication reporting."""

from __future__ import annotations

import argparse

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import auth_token
from tests.harness.init_agent_support import (
    api_key,
    credential_reads,
    empty_host,
    login,
    project_config,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    return empty_host(tmp_path, monkeypatch)


def _auth_step(selection, **kwargs):
    """Run init's auth step and return the record it left."""
    ctx = InitContext(show_step_banners=False)
    # private: `_step_auth` runs only inside full host init, between Host Bootstrap and
    # image reconciliation; reaching it through `run_init` would replace every later step.
    init_cmd._step_auth(ctx, selection, **kwargs)
    return ctx.results[-1]


@pytest.mark.parametrize(
    ("logins", "keys", "selection", "skip", "record", "present", "absent"),
    [
        pytest.param(
            ("claude",),
            ("codex",),
            ("claude", "subscription"),
            False,
            ("auth", "ok", "claude/subscription"),
            (
                "configured agent: claude/subscription",
                "detected subscription auth for claude",
                "using ",
            ),
            ("codex",),
            id="configured-provider-only",
        ),
        pytest.param(
            ("claude",),
            ("claude",),
            ("claude", "subscription"),
            False,
            ("auth", "ok", "claude/subscription"),
            ("detected subscription auth for claude",),
            ("ANTHROPIC_API_KEY",),
            id="policy-picks-subscription",
        ),
        pytest.param(
            ("claude",),
            ("claude",),
            ("claude", "api_key"),
            False,
            ("auth", "ok", "claude/api_key"),
            (
                "detected api_key auth for claude",
                "using ANTHROPIC_API_KEY (exported)",
                "is present but not used; the line above wins",
            ),
            (),
            id="key-bills-over-login",
        ),
        pytest.param(
            (),
            (),
            ("codex", "subscription"),
            False,
            ("auth", "warn", "codex/subscription unavailable"),
            ("no subscription auth available for codex",),
            (),
            id="no-credential-warns",
        ),
        pytest.param(
            ("codex",),
            (),
            ("codex", "subscription"),
            True,
            ("auth", "skip", "credential check skipped"),
            ("credential check skipped by --skip-credentials",),
            ("detected", "no subscription auth"),
            id="skip-credentials",
        ),
    ],
)
def test_auth_step_reports_the_credential_that_bills(
    _home, monkeypatch, capsys, logins, keys, selection, skip, record, present, absent
):
    for cli in logins:
        login(_home, cli)
    for cli in keys:
        api_key(monkeypatch, cli)

    with credential_reads(_home) as opened:
        result = _auth_step(init_cmd.AgentSelection(*selection), skip_credentials=skip)

    assert (result.name, result.status, result.detail) == record
    out = capsys.readouterr().out
    for text in present:
        assert text in out
    for text in absent:
        assert text not in out
    # Skipping the check must not even read a credential (commit 1616d0dae).
    assert (opened == []) is skip
    if "is present but not used; the line above wins" in present:
        lines = out.splitlines()
        winner = next(i for i, line in enumerate(lines) if "using ANTHROPIC_API_KEY" in line)
        hint = lines[winner + 1].lstrip()
        assert hint.startswith("→ the subscription login at ")
        assert hint.endswith("is present but not used; the line above wins")


def test_auth_step_reports_every_allowed_cli_under_its_policy(_home, tmp_path, capsys):
    login(_home, "claude")
    config = project_config(
        tmp_path / "project",
        b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
        b'[agent.auth]\nclaude = "subscription"\ncodex = "api_key"\n',
    )
    # private: the selection must come from the resolver, as host init builds it;
    # a hand-built `AgentSelection` cannot show the resolver keeping the other CLIs.
    selection = init_cmd._resolve_agent_selection(
        InitContext(show_step_banners=False), argparse.Namespace(), config
    )
    assert selection is not None

    result = _auth_step(selection)

    assert (result.name, result.status, result.detail) == (
        "auth",
        "warn",
        "claude/subscription; codex/api_key unavailable",
    )
    out = capsys.readouterr().out
    assert "detected subscription auth for claude" in out
    assert "no api_key auth available for codex" in out


@pytest.mark.parametrize(
    ("holds", "models", "detail", "code", "missing"),
    [
        pytest.param(
            "codex",
            b"",
            "claude/subscription unavailable; codex/api_key",
            1,
            "no subscription auth available for claude",
            id="default-missing",
        ),
        pytest.param(
            "claude",
            b'[models]\nreviewer = "gpt-5"\n',
            "claude/subscription; codex/api_key unavailable",
            1,
            "no api_key auth available for codex",
            id="specialist-cli-missing",
        ),
        pytest.param(
            "claude",
            b"",
            "checked authentication: claude/subscription; codex/api_key unavailable",
            0,
            "no api_key auth available for codex",
            id="optional-cli-missing",
        ),
    ],
)
def test_check_only_exit_follows_required_clis(
    _home, monkeypatch, tmp_path, capsys, holds, models, detail, code, missing
):
    if holds == "claude":
        login(_home, "claude")
    else:
        api_key(monkeypatch, "codex")
    config = project_config(
        tmp_path / "project",
        b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
        b'[agent.auth]\nclaude = "subscription"\ncodex = "api_key"\n' + models,
    )
    ctx = InitContext(check_only=True, show_step_banners=False)
    selection = init_cmd._resolve_agent_selection(ctx, argparse.Namespace(), config)
    assert selection is not None

    init_cmd._step_auth(ctx, selection)
    # private: the closing step of a run whose earlier steps need Docker.
    exit_code = init_cmd._print_summary(ctx)

    assert (ctx.results[-1].status, ctx.results[-1].detail) == (
        "ok" if code == 0 else "warn",
        detail,
    )
    assert exit_code == code
    assert missing in capsys.readouterr().out


@pytest.mark.parametrize("external", [False, True])
def test_issued_policy_uses_authorized_source_not_ambient_auth_handoff(
    tmp_path, monkeypatch, external
):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_SECRET")
    data = tmp_path / "project" / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text('[agent]\nprovider="claude"\nauth="api_key"\n')
    selected = data
    if external:
        selected = tmp_path / "trusted-external-data"
        selected.mkdir()
        (selected / "booley.toml").write_text((data / "booley.toml").read_text())
        (data / "booley.toml").write_text('[agent]\nprovider="claude"\nauth="subscription"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(selected))
    # private: the public `current_interactive_spec` previews the spec through Docker.
    sources = init_cmd._sandbox_spec_sources(
        InitContext(project_root=data.parent),
        init_cmd._NANGATE_PDK_NOT_REQUESTED,  # private: same reason as the call above
        "claude",
        None,
    )
    assert sources.project_data_source == selected
    (plan,) = sources.credential_plans
    assert plan.policy == "api_key"
    assert plan.selected.mode == auth_token.MODE_API_KEY
    assert plan.subscription_seed is None
