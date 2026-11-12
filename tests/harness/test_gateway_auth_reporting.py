"""Doctor, ``booley auth --status`` and credential scrubbing under the gateway policy (#2266)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import pytest

from booley.config import agent as agent_config
from booley.core.models import AgentCallParams
from booley.harness import auth_cmd, doctor
from booley.runtime import _claude_backend, _codex_backend, auth_token
from tests.gateway_command_support import write_token_command

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"
_BEARER = "SYNTHETIC_ANTHROPIC_BEARER"


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    """Sandbox-like process with the gateway policy selected and a complete config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _provider: True)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", _TOKEN)
    return tmp_path


def _doctor_findings(root, provider="codex"):
    """Run the agent credential checks for a Project and return its findings."""
    data = root / ".booley"
    data.mkdir(parents=True, exist_ok=True)
    audit = doctor.ProjectAudit(
        project_root=root,
        booley_dir=data,
        booley_toml={"agent": {"provider": provider, "auth": "subscription"}},
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    return reporter.findings or []


def _auth_status(root, capsys, app="codex"):
    code = auth_cmd.run_auth(argparse.Namespace(status=True, app=app), root)
    return code, capsys.readouterr()


def test_doctor_gateway_complete_config_passes_without_rotation_risk(gateway_env):
    findings = _doctor_findings(gateway_env)

    assert [f.severity.value for f in findings] == ["pass"]
    assert f"the gateway at {_URL}" in findings[0].message
    assert not any(f.check_id == "agent.rotation-risk" for f in findings)
    assert _TOKEN not in str([f.message for f in findings])


def test_doctor_gateway_missing_url_fails(gateway_env, monkeypatch):
    monkeypatch.delenv("BOOLEY_GATEWAY_URL")

    findings = _doctor_findings(gateway_env)

    assert [(f.severity.value, f.check_id) for f in findings] == [
        ("fail", "agent.credential-missing")
    ]
    assert "BOOLEY_GATEWAY_URL" in findings[0].message


def test_doctor_gateway_missing_token_fails(gateway_env, monkeypatch):
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")

    findings = _doctor_findings(gateway_env)

    assert [(f.severity.value, f.check_id) for f in findings] == [
        ("fail", "agent.credential-missing")
    ]
    assert "BOOLEY_GATEWAY_TOKEN" in findings[0].message


@pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")
def test_doctor_gateway_failing_token_command_fails_quoting_stderr(gateway_env, monkeypatch):
    instruction = "your gateway login expired: run `gateway-login --device`"
    command, _ = write_token_command(
        gateway_env, ["SYNTHETIC_LEAKED_STDOUT"], stderr=instruction + "\n", exit_code=2
    )
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)

    findings = _doctor_findings(gateway_env)

    assert [(f.severity.value, f.check_id) for f in findings] == [
        ("fail", "agent.credential-missing")
    ]
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND exited 2" in findings[0].message
    assert instruction in findings[0].message
    assert "SYNTHETIC_LEAKED_STDOUT" not in findings[0].message


@pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")
def test_doctor_gateway_auto_fallback_without_token_warns(gateway_env, monkeypatch):
    """Under BOOLEY_AGENT_APP=auto the gateway is a fallback: no token yet is a warning."""
    from booley.config import agent_app

    command, _ = write_token_command(gateway_env, [], stderr="no token yet\n", exit_code=1)
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)
    choice = agent_app.AgentAppChoice(
        "codex", "gateway", "no Claude or Codex credential", ("codex",)
    )
    monkeypatch.setattr(agent_app, "auto_choice", lambda: choice)

    findings = _doctor_findings(gateway_env)

    assert [(f.severity.value, f.check_id) for f in findings if f.severity.value != "pass"] == [
        ("warn", "agent.credential-missing")
    ]
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND exited 1" in str(findings[-1].message)


@pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")
def test_doctor_gateway_working_token_command_passes(gateway_env, monkeypatch):
    command, _ = write_token_command(gateway_env, [_TOKEN])
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)

    findings = _doctor_findings(gateway_env)

    assert [f.severity.value for f in findings] == ["pass"]
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND" in findings[0].message
    assert _TOKEN not in findings[0].message


def test_gateway_handoff_ignored_on_host_with_doctor_warning(gateway_env, monkeypatch):
    monkeypatch.setattr(agent_config, "_inside_container", lambda: False)

    findings = _doctor_findings(gateway_env)

    warned = [f for f in findings if f.check_id == "agent.credential-policy"]
    assert [f.severity.value for f in warned] == ["warn"]
    assert "gateway" not in " ".join(f.message for f in findings if f.severity.value == "pass")


def test_auth_status_gateway_complete_config_exits_zero(gateway_env, capsys):
    code, captured = _auth_status(gateway_env, capsys)

    assert code == 0
    assert f"the gateway at {_URL}" in captured.out
    assert _TOKEN not in captured.out + captured.err


def test_auth_status_states_the_policy_in_words_with_one_glyph_per_line(
    gateway_env, monkeypatch, capsys
):
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", False)

    _code, captured = _auth_status(gateway_env, capsys)

    lines = captured.out.splitlines()
    assert lines[:3] == [
        "",
        "Agent auth status",
        "  \u00b7  auth policy 'gateway'; credentials come from the environment this command inherited",
    ]
    assert lines[3].startswith(f"  \u2713  codex: agents run on the gateway at {_URL}")


def test_gateway_with_claude_provider_fails_doctor_and_status(gateway_env, capsys):
    findings = _doctor_findings(gateway_env, provider="claude")
    (gateway_env / ".booley" / "booley.toml").write_text(
        '[agent]\nprovider = "claude"\n', encoding="utf-8"
    )
    code, _ = _auth_status(gateway_env, capsys, app=None)

    assert [(f.severity.value, f.check_id) for f in findings] == [
        ("fail", "agent.credential-missing")
    ]
    assert code == 1


def test_auth_status_gateway_missing_token_exits_one_naming_it(gateway_env, monkeypatch, capsys):
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")

    code, captured = _auth_status(gateway_env, capsys)

    assert code == 1
    assert "BOOLEY_GATEWAY_TOKEN" in captured.out + captured.err


def test_subscription_removes_anthropic_auth_token_from_claude_env(gateway_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", _BEARER)

    overrides = _claude_backend._claude_env_overrides("subscription")

    assert overrides["ANTHROPIC_AUTH_TOKEN"] == ""


def test_subscription_removes_anthropic_auth_token_from_codex_child_env(gateway_env, monkeypatch):
    monkeypatch.setattr(_codex_backend, "_inside_container", lambda: False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", _BEARER)
    login = auth_token.subscription_creds_path("codex")
    login.parent.mkdir(parents=True)
    login.write_text(json.dumps({"tokens": {"access_token": "SYNTHETIC_LOGIN"}}), encoding="utf-8")
    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    params = AgentCallParams(prompt="p", model="gpt-5.5", cwd=".")

    asyncio.run(_codex_backend._codex_spawn(["codex"], params, "subscription"))

    assert captured["env"] is not None
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]


def test_auto_keeps_anthropic_auth_token_for_claude_but_never_forwards_it(
    gateway_env, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", _BEARER)

    plan = auth_token.credential_plan("claude", "auto")

    assert "ANTHROPIC_AUTH_TOKEN" not in _claude_backend._claude_env_overrides("auto")
    assert "ANTHROPIC_AUTH_TOKEN" in plan.allowed_env
    assert "ANTHROPIC_AUTH_TOKEN" not in dict(plan.values)
    assert _BEARER not in repr(plan)


def test_auth_status_notes_a_gateway_policy_it_ignores(gateway_env, monkeypatch, capsys):
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: False)

    _code, captured = _auth_status(gateway_env, capsys, app="claude")

    assert (
        "  \u00b7  BOOLEY_PRIMARY_AUTH=gateway is ignored outside a Sandbox that sets it"
        in captured.out
    )


# ---------------------------------------------------------------------------
# Per-CLI Codex gateway in an issued Sandbox: Claude sessions never see it (#2897)
# ---------------------------------------------------------------------------

_RECORD = "claude=subscription,codex=gateway"
_HIDDEN_NOTE = "gateway settings are hidden from Claude sessions"
_GATEWAY_KEYS = (
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)


@pytest.fixture
def claude_session(gateway_env, monkeypatch):
    """An issued ``codex=gateway`` Sandbox seen from a Claude session: values blank, marker set."""
    monkeypatch.setenv("BOOLEY_AGENT_CLIS", _RECORD)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    for key in _GATEWAY_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("BOOLEY_HIDDEN_GATEWAY", "1")
    return gateway_env


def _write_toml(root, text):
    data = root / ".booley"
    data.mkdir(parents=True, exist_ok=True)
    (data / "booley.toml").write_bytes(text.encode("utf-8"))


def test_doctor_in_claude_session_notes_hidden_gateway(claude_session, monkeypatch):
    findings = _doctor_findings(claude_session)

    assert [f.severity.value for f in findings] == ["note"]
    assert _HIDDEN_NOTE in findings[0].message

    # A VS Code terminal whose localEnv was empty has no marker: that Sandbox cannot reach it.
    monkeypatch.delenv("BOOLEY_HIDDEN_GATEWAY")
    findings = _doctor_findings(claude_session)
    assert [(f.severity.value, f.check_id) for f in findings] == [
        ("fail", "agent.credential-missing")
    ]


def test_auth_status_in_claude_session_notes_hidden_gateway(claude_session, monkeypatch, capsys):
    code, captured = _auth_status(claude_session, capsys)

    assert code == 0
    assert _HIDDEN_NOTE in captured.out

    monkeypatch.delenv("BOOLEY_HIDDEN_GATEWAY")
    code, captured = _auth_status(claude_session, capsys)
    assert code == 1
    assert "BOOLEY_GATEWAY_URL" in captured.out + captured.err


def _deep_probe_failure(project, monkeypatch):
    """Run the developer probe with a provider call that fails on the gateway configuration."""
    from booley.harness import developer_probe
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)

    def fail(_root):
        raise developer_probe.ProbeError(
            "probe agent failed: BOOLEY_GATEWAY_URL is not set", agent_failure=True
        )

    monkeypatch.setattr(developer_probe, "measure_developer_rss", fail)
    events: list[tuple[str, str]] = []

    def record(kind):
        return lambda message, *_fix: events.append((kind, message))

    doctor._run_developer_probe(project, record("pass"), record("skip"), record("fail"))
    return events


def test_doctor_deep_in_claude_session_skips_gateway_developer_probe(claude_session, monkeypatch):
    _write_toml(claude_session, '[agent]\ndefault = "codex"\nclis = ["claude", "codex"]\n')
    project = doctor.ProjectAudit(
        project_root=claude_session,
        booley_dir=claude_session / ".booley",
        booley_toml={"agent": {"default": "codex", "clis": ["claude", "codex"]}},
        configs_toml={},
        first_target="",
    )

    events = _deep_probe_failure(project, monkeypatch)

    assert [kind for kind, _ in events] == ["skip"]
    assert _HIDDEN_NOTE in events[0][1]

    monkeypatch.delenv("BOOLEY_HIDDEN_GATEWAY")
    events = _deep_probe_failure(project, monkeypatch)
    assert [kind for kind, _ in events] == ["fail"]

    # The internal model on Claude is not the gateway's: the failure stays a FAIL.
    monkeypatch.setenv("BOOLEY_HIDDEN_GATEWAY", "1")
    _write_toml(claude_session, '[agent]\ndefault = "claude"\nclis = ["claude", "codex"]\n')
    assert [kind for kind, _ in _deep_probe_failure(project, monkeypatch)] == ["fail"]

    # An unreadable Project config is graded by its own check, not hidden behind the note.
    def unreadable(*_args, **_kwargs):
        raise agent_config.BackendConfigError("cannot read booley.toml")

    monkeypatch.setattr(agent_config, "load_agent_settings", unreadable)
    assert [kind for kind, _ in _deep_probe_failure(project, monkeypatch)] == ["fail"]


def test_auth_status_for_claude_ignores_codex_gateway(claude_session, monkeypatch, capsys):
    monkeypatch.delenv("BOOLEY_HIDDEN_GATEWAY")
    _write_toml(
        claude_session,
        '[agent]\ndefault = "claude"\nclis = ["claude", "codex"]\n'
        '[agent.auth]\nclaude = "subscription"\ncodex = "gateway"\n',
    )

    _code, captured = _auth_status(claude_session, capsys, app="claude")

    output = captured.out + captured.err
    assert "auth policy 'subscription'" in output
    assert "claude:" in output
    assert "the gateway at" not in output


def test_auth_status_on_host_reads_declared_gateway_as_auto(gateway_env, monkeypatch, capsys):
    monkeypatch.setattr(agent_config, "_inside_container", lambda: False)
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-synthetic-host-key")
    _write_toml(
        gateway_env,
        '[agent]\ndefault = "codex"\nclis = ["claude", "codex"]\n[agent.auth]\ncodex = "gateway"\n',
    )

    code, captured = _auth_status(gateway_env, capsys)

    output = captured.out + captured.err
    assert code == 0
    assert "applies inside a Sandbox only; showing 'auto'" in output
    assert "auth policy 'auto'" in output
    assert "codex: agents run on" in output


def test_auth_status_in_handwritten_sandbox_reports_declared_gateway(
    gateway_env, monkeypatch, capsys
):
    """No record and no hand-off: a declared `codex = "gateway"` is still the gateway."""
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH")
    _write_toml(
        gateway_env,
        '[agent]\ndefault = "codex"\nclis = ["claude", "codex"]\n[agent.auth]\ncodex = "gateway"\n',
    )

    code, captured = _auth_status(gateway_env, capsys)

    output = captured.out + captured.err
    assert code == 0
    assert "agents run on the gateway at" in output
    assert "applies inside a Sandbox only" not in output


def test_claude_sdk_env_blanks_gateway_settings(gateway_env, monkeypatch):
    monkeypatch.setenv("BOOLEY_AGENT_CLIS", _RECORD)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "synthetic-model")
    monkeypatch.delenv("BOOLEY_GATEWAY_EFFORT", raising=False)

    overrides = _claude_backend._claude_env_overrides("subscription")

    blanked = {key for key, value in overrides.items() if key.startswith("BOOLEY_") and not value}
    assert blanked == {"BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_TOKEN", "BOOLEY_GATEWAY_MODEL"}
    assert overrides["BOOLEY_HIDDEN_GATEWAY"] == "1"


def test_claude_sdk_env_without_gateway_values_sets_no_marker(gateway_env, monkeypatch):
    monkeypatch.setenv("BOOLEY_AGENT_CLIS", _RECORD)
    for key in _GATEWAY_KEYS:
        monkeypatch.delenv(key, raising=False)

    overrides = _claude_backend._claude_env_overrides("subscription")

    assert "BOOLEY_HIDDEN_GATEWAY" not in overrides
