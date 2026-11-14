"""``BOOLEY_AGENT_APP=auto``: the registrar picks the app and every consumer agrees (#2425).

Every test runs as a Sandbox start: the registrar hook runs, then the Specialist,
MCP-server, ``booley chat`` and Doctor consumers read what the start decided.
Nothing here depends on host credentials or on ``claude``/``codex`` being on PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.devcontainer_spec_support import build_spec

from booley.config import agent as config_agent
from booley.config import agent_app
from booley.config.agent import BackendConfigError, get_agent_settings, load_agent_settings
from booley.config.settings import set_agent_settings
from booley.harness import auth_cmd, chat_cmd, doctor
from booley.harness import incontainer_register as entry
from booley.runtime import devcontainer as dc
from booley.runtime import incontainer_setup as reg
from booley.runtime import inspection

_GATEWAY_URL = "https://gateway.example/v1"
_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_HOME",
    "BOOLEY_PRIMARY_AUTH",
    "BOOLEY_PRIMARY_PROVIDER",
    "BOOLEY_DIR",
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)


def _use_home(monkeypatch, home: Path) -> Path:
    """Make *home* the Sandbox home on POSIX (HOME) and Windows (USERPROFILE)."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An isolated in-Sandbox start with ``BOOLEY_AGENT_APP=auto`` and no credential."""
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("BOOLEY_AGENT_APP", "auto")
    monkeypatch.setattr(config_agent, "_inside_container", lambda: True)
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _provider: True)
    monkeypatch.setattr(reg, "ensure_http_server", lambda *, mode: "started")
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "started")
    monkeypatch.setattr(shutil, "which", lambda name, mode=None: None)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    set_agent_settings(None)
    yield SimpleNamespace(home=_use_home(monkeypatch, tmp_path / "home"), project=project)
    set_agent_settings(None)


def _give(kind: str, home: Path, monkeypatch) -> None:
    """Provide the credential situation *kind* in the Sandbox."""
    if kind in ("claude-api-key", "both"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-claude-key")
    if kind == "claude-oauth-token":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-claude-token")
    if kind == "claude-login":
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text("{}")
    if kind in ("codex-api-key", "both"):
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-codex-key")
    if kind == "codex-login":
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text('{"tokens": {}}')
    if kind == "gateway":
        monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "synthetic-gateway-token")
        monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "synthetic-gateway-model")


def _start(capsys) -> str:
    """Run the registrar hook once and return its status line."""
    entry.main()
    return capsys.readouterr().err


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _claude_registered(home: Path) -> bool:
    config = _read(home / ".claude.json")
    return "booley" in json.loads(config).get("mcpServers", {}) if config else False


def _codex_config(home: Path) -> str:
    return _read(home / ".codex" / "config.toml")


def _refusal(action) -> str:
    """The message of the ``BackendConfigError`` *action* raises, or ``""``."""
    try:
        action()
    except BackendConfigError as exc:
        return str(exc)
    return ""


# What each credential situation resolves to: (default app, policy the consumers see,
# every allowed CLI with the default first).
_CASES = {
    "claude-api-key": ("claude", "auto", ("claude",)),
    "claude-oauth-token": ("claude", "auto", ("claude",)),
    "claude-login": ("claude", "auto", ("claude",)),
    "codex-api-key": ("codex", "auto", ("codex",)),
    "codex-login": ("codex", "auto", ("codex",)),
    "both": ("claude", "auto", ("claude", "codex")),
    "gateway": ("codex", "gateway", ("codex",)),
    "nothing": ("claude", "auto", ("claude",)),
}


@pytest.mark.parametrize("kind", list(_CASES))
def test_auto_choice_reaches_every_consumer(sandbox, monkeypatch, capsys, kind):
    app, policy, clis = _CASES[kind]
    _give(kind, sandbox.home, monkeypatch)

    status = _start(capsys)

    assert f"app:{app} (BOOLEY_AGENT_APP=auto: " in status
    assert (" clis:claude,codex" in status) == (len(clis) > 1)
    assert agent_app.auto_choice().clis == clis
    assert config_agent.sandbox_agent_clis() == clis
    assert _claude_registered(sandbox.home) == ("claude" in clis)
    assert ("[mcp_servers.booley]" in _codex_config(sandbox.home)) == ("codex" in clis)
    if app == "codex":
        assert ('model_provider = "booley_gateway"' in _codex_config(sandbox.home)) == (
            policy == "gateway"
        )
    for read_settings in (lambda: None, lambda: load_agent_settings(sandbox.project)):
        set_agent_settings(None)  # Specialists load lazily; the MCP server loads the Project
        read_settings()
        settings = get_agent_settings()
        assert (settings.default_cli, settings.auth_for(app)) == (app, policy)
    assert doctor._configured_provider(None) == app
    assert doctor._configured_auth_policy(None) == policy
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    assert chat_cmd.run(argparse.Namespace(agent_args=[]), sandbox.project) == 2
    assert f"configured agent CLI '{app}'" in capsys.readouterr().err


def test_consumers_keep_the_start_choice_when_a_credential_appears_later(
    sandbox, monkeypatch, capsys
):
    _give("gateway", sandbox.home, monkeypatch)
    _start(capsys)

    _give("claude-login", sandbox.home, monkeypatch)  # appears after the start resolved

    assert doctor._configured_provider(None) == "codex"
    assert doctor._configured_auth_policy(None) == "gateway"
    assert (get_agent_settings().default_cli, get_agent_settings().auth_for("codex")) == (
        "codex",
        "gateway",
    )
    assert config_agent.env_auth_handoff() == "gateway"


def test_signing_in_after_a_gateway_start_makes_the_stored_health_result_due(
    sandbox, monkeypatch, capsys
):
    """A Doctor result graded against the old app must not read as current health.

    Codespaces visitor: the first start chose the gateway and Doctor WARNed about
    its missing token; the visitor then signed in and re-ran the registrar. A
    result from a run that started before the sign-in stays due even when it
    lands afterwards (an automatic Doctor still in flight).
    """
    from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
    from booley.runtime import health
    from booley.runtime.booley_dir import reset_cache

    booley_dir = sandbox.project / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text('[project]\nname="auto-health"\n')
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()

    def publish_token_warning(started_at: str) -> None:
        finding = DiagnosticFinding(
            Severity.WARN, "gateway token command exited 1", check_id="agent.gateway-token"
        )
        health.publish_result(
            sandbox.project,
            trigger="test",
            duration_s=0,
            diagnostics=DiagnosticReport((finding,)),
            transcript="",
            started_at=started_at,
        )

    _give("gateway", sandbox.home, monkeypatch)
    _start(capsys)
    in_flight_start = health.run_start_stamp()
    publish_token_warning(in_flight_start)

    _start(capsys)  # an unchanged choice keeps the result current
    assert health.due_reason(sandbox.project) is None

    _give("codex-api-key", sandbox.home, monkeypatch)  # the visitor signs in
    assert "app:codex (BOOLEY_AGENT_APP=auto: " in _start(capsys)
    changed = "agent app changed since this check"
    assert health.due_reason(sandbox.project) == changed

    publish_token_warning(in_flight_start)  # the in-flight run lands late
    assert health.due_reason(sandbox.project) == changed

    publish_token_warning(health.run_start_stamp())  # a run started after the sign-in
    assert health.due_reason(sandbox.project) is None
    reset_cache()


@pytest.mark.parametrize(
    "credential,gateway_policy",
    [("claude-api-key", None), ("codex-api-key", None), ("codex-api-key", "gateway")],
    ids=["claude", "codex", "codex-gateway-policy"],
)
def test_visitor_credential_beats_a_configured_gateway(
    sandbox, monkeypatch, capsys, credential, gateway_policy
):
    _give("gateway", sandbox.home, monkeypatch)
    _give(credential, sandbox.home, monkeypatch)
    if gateway_policy:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", gateway_policy)
    app = credential.split("-")[0]

    status = _start(capsys)

    assert f"app:{app} " in status
    assert not config_agent.gateway_handoff_active()
    assert set(get_agent_settings().auth.values()) == {"auto"}
    assert "booley_gateway" not in _codex_config(sandbox.home)


def _explicit_files(home: Path) -> dict[str, str]:
    """The user config the registrar wrote, with the home path made comparable."""
    names = (".claude.json", ".claude/settings.json", ".codex/config.toml", ".codex/auth.json")
    # TOML and JSON escape each Windows backslash, so replace the escaped form first.
    escaped = str(home).replace("\\", "\\\\")
    return {
        name: _read(home / name).replace(escaped, "<HOME>").replace(str(home), "<HOME>")
        for name in names
    }


@pytest.mark.parametrize(
    "kind", ["claude-api-key", "codex-api-key", "gateway"], ids=["claude", "codex", "gateway"]
)
def test_auto_registration_matches_the_explicit_app(sandbox, monkeypatch, capsys, tmp_path, kind):
    app, policy, _clis = _CASES[kind]
    _give(kind, sandbox.home, monkeypatch)
    _start(capsys)
    resolved = _explicit_files(sandbox.home)

    explicit_home = _use_home(monkeypatch, tmp_path / "explicit-home")
    monkeypatch.setenv("BOOLEY_AGENT_APP", app)
    if policy == "gateway":
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    _start(capsys)

    assert any(resolved.values())
    assert resolved == _explicit_files(explicit_home)


def _restart_with(kind: str, sandbox, monkeypatch, capsys) -> None:
    """A second start in the same Sandbox after the credentials changed to *kind*."""
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for stale in (".claude/.credentials.json", ".codex/auth.json"):
        (sandbox.home / stale).unlink(missing_ok=True)
    _give(kind, sandbox.home, monkeypatch)
    _start(capsys)


@pytest.mark.parametrize(
    "first,second",
    [
        ("gateway", "codex-api-key"),
        ("claude-api-key", "codex-api-key"),
        ("codex-api-key", "claude-api-key"),
    ],
    ids=["gateway-to-codex-key", "claude-to-codex", "codex-to-claude"],
)
def test_restart_after_the_choice_changes_leaves_no_breaking_registration(
    sandbox, monkeypatch, capsys, tmp_path, first, second
):
    _give(first, sandbox.home, monkeypatch)
    _start(capsys)

    _restart_with(second, sandbox, monkeypatch, capsys)

    app = _CASES[second][0]
    assert doctor._configured_provider(None) == app
    codex = _codex_config(sandbox.home)
    assert "booley_gateway" not in codex
    assert not any(line.startswith("model") for line in codex.split("[", 1)[0].splitlines())
    resolved_file = ".claude.json" if app == "claude" else ".codex/config.toml"
    if first != "gateway":  # the gateway start leaves its non-routing codex keys behind
        fresh_home = _use_home(monkeypatch, tmp_path / "fresh-home")
        monkeypatch.setenv("BOOLEY_AGENT_APP", app)
        _start(capsys)
        assert (
            _explicit_files(sandbox.home)[resolved_file]
            == _explicit_files(fresh_home)[resolved_file]
        )
    else:
        assert "[mcp_servers.booley]" in codex


def _doctor_findings(project) -> list:
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(project, reporter)
    return reporter.findings


@pytest.mark.parametrize("kind", ["both", "gateway"])
def test_doctor_reports_the_auto_choice_and_accepts_an_unpinned_auto_spec(
    sandbox, monkeypatch, capsys, kind
):
    _give(kind, sandbox.home, monkeypatch)
    _start(capsys)
    app, _policy, _clis = _CASES[kind]

    findings = _doctor_findings(SimpleNamespace(booley_toml={"agent": {}}))

    chosen = [f.message for f in findings if "BOOLEY_AGENT_APP=auto" in f.message]
    assert len(chosen) == 1
    assert ("Claude Code" if app == "claude" else "Codex") in chosen[0]
    assert ("Claude is the default" in chosen[0]) == (kind == "both")
    configuration = _auto_spec_report(sandbox, monkeypatch, declared_provider=None)
    # An unpinned auto spec is accepted: no WARN or FAIL, only the note that it is
    # hand-written and so not compared with what `booley init` would write.
    assert [(f.severity.value, "hand-written" in f.message) for f in configuration.findings] == [
        ("note", True)
    ]


def test_doctor_audits_every_auto_allowed_cli(sandbox, monkeypatch, capsys):
    _give("both", sandbox.home, monkeypatch)
    _start(capsys)

    findings = _doctor_findings(SimpleNamespace(booley_toml={"agent": {}}))

    messages = [f.message for f in findings]
    chosen = [m for m in messages if "BOOLEY_AGENT_APP=auto" in m]
    assert len(chosen) == 1
    assert "Claude Code (default), Codex" in chosen[0]
    # One credential line per allowed CLI, not only the default's.
    assert any(m.startswith("Claude Code auth:") for m in messages)
    assert any(m.startswith("Codex auth:") for m in messages)


def _auto_spec_report(sandbox, monkeypatch, *, declared_provider):
    """The ``.configuration`` report for a generated spec whose app is ``auto``."""
    monkeypatch.setattr(inspection.idk, "image_id", lambda image: image)
    spec = build_spec("claude")
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = "auto"
    dc.write_devcontainer(sandbox.project, spec)
    request = inspection.RuntimeInspectionRequest(
        sandbox.project, dc.SANDBOX_IMAGE, None, declared_provider=declared_provider
    )
    return inspection.inspect_runtime(request).configuration


def test_auto_spec_with_a_provider_pin_warns_instead_of_failing(sandbox, monkeypatch):
    report = _auto_spec_report(sandbox, monkeypatch, declared_provider="codex")

    findings = report.findings
    assert "fail" not in {f.severity.value for f in findings}
    assert [
        f
        for f in findings
        if f.severity.value == "warn" and "BOOLEY_AGENT_APP 'auto'" in f.message
    ]


@pytest.mark.parametrize(
    "surface", ["specialist-settings", "project-settings", "doctor", "registrar", "auth-status"]
)
def test_auto_outside_a_sandbox_is_refused_naming_the_variable(
    sandbox, monkeypatch, capsys, surface
):
    monkeypatch.setattr(config_agent, "_inside_container", lambda: False)
    if surface == "specialist-settings":
        message = _refusal(get_agent_settings)
    elif surface == "project-settings":
        message = _refusal(lambda: load_agent_settings(sandbox.project))
    elif surface == "doctor":
        message = " ".join(f.message for f in _doctor_findings(None) if f.severity.value == "fail")
    elif surface == "registrar":
        code = None
        try:
            entry.main()
        except SystemExit as exit_info:
            code = exit_info.code
        message = capsys.readouterr().err
        assert (code, "registration refused" in message) == (1, True)
    else:
        auth_cmd.run_auth(argparse.Namespace(status=True, app=None), None)
        captured = capsys.readouterr()
        message = captured.out + captured.err
    assert "BOOLEY_AGENT_APP" in message


@pytest.mark.parametrize(
    "record", [None, "not json", '{"schema": 99}'], ids=["missing", "unreadable", "unknown-schema"]
)
def test_unresolved_auto_is_refused_naming_the_start_hook(sandbox, record):
    if record is not None:
        (sandbox.home / ".booley-agent-app.json").write_text(record)

    message = _refusal(get_agent_settings)

    assert "BOOLEY_AGENT_APP" in message
    assert "incontainer_register" in message


def test_failed_resolution_leaves_no_stale_choice(sandbox, monkeypatch, capsys):
    _give("claude-api-key", sandbox.home, monkeypatch)
    _start(capsys)

    def broken_store(*_args, **_kwargs):
        raise RuntimeError("credential store unreadable")

    monkeypatch.setattr(reg.auth_token, "effective_credential", broken_store)
    with contextlib.suppress(RuntimeError):  # the hook ends here; consumers see what it left
        entry.main()

    message = _refusal(get_agent_settings)
    assert "incontainer_register" in message


@pytest.mark.parametrize("kind", ["claude-api-key", "gateway"])
def test_handwritten_auto_policy_selects_the_visitor_mode(sandbox, monkeypatch, capsys, kind):
    """``BOOLEY_PRIMARY_AUTH=auto``: the visitor's credential, else the sponsored gateway."""
    from booley.runtime import auth_token

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "auto")
    _give(kind, sandbox.home, monkeypatch)
    app, policy = ("claude", "visitor") if kind == "claude-api-key" else ("codex", "gateway")

    status = _start(capsys)

    assert f"app:{app} " in status
    assert get_agent_settings().auth_for(app) == policy
    if kind == "claude-api-key":
        plan = auth_token.credential_plan("claude", policy)
        assert [key for key, _ in plan.values] == ["ANTHROPIC_API_KEY"]
        assert "booley_gateway" not in _codex_config(sandbox.home)
    else:
        assert 'model_provider = "booley_gateway"' in _codex_config(sandbox.home)


@pytest.mark.parametrize("auth", [None, "auto", "gateway", "subscription"])
def test_gateway_fallback_keys_on_the_visitor_mode(sandbox, monkeypatch, auth):
    """The gateway fallback depends on the visitor mode, not on the ``auto`` config policy."""
    from booley.runtime import auth_token

    lookups: list[str] = []
    real = auth_token.login_credential

    def spy(app, policy="auto", *, home=None):
        lookups.append(policy)
        return real(app, policy, home=home)

    monkeypatch.setattr(auth_token, "login_credential", spy)
    _give("gateway", sandbox.home, monkeypatch)
    if auth:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", auth)

    choice = entry.resolve_auto_choice()

    if auth == "subscription":
        assert (choice.app, choice.policy) == ("claude", "subscription")
        assert set(lookups) == {"subscription"}
    else:
        assert (choice.app, choice.policy) == ("codex", "gateway")
        assert set(lookups) == {"visitor"}
