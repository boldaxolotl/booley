"""Doctor audits the credential of every Agent CLI a Project allows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import tomlkit

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.runtime import auth_token
from booley.runtime import devcontainer as dc
from tests.gateway_command_support import write_token_command
from tests.harness.agent_auth_support import install_agent_clis, pin_venue
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.doctor_run_support import run_doctor, write_project

_CLAUDE_LOGIN = {
    "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 4102444800000}
}
_CODEX_LOGIN = {"tokens": {"access_token": "a", "refresh_token": "r"}}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A host Project with no credentials in the environment or on disk, both CLIs installed."""
    patch_doctor_environment(monkeypatch, tmp_path, write_project(tmp_path))
    home = Path.home()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    ambient = "CLAUDE_CODE_OAUTH_TOKEN CODEX_HOME BOOLEY_PRIMARY_PROVIDER BOOLEY_PRIMARY_AUTH"
    for name in (*auth_token.CREDENTIAL_ENV_KEYS, *ambient.split(), "BOOLEY_AGENT_CLIS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
    pin_venue(monkeypatch, inside=False)
    install_agent_clis(home)
    return home


def _login(home: Path, cli: str) -> None:
    """Leave *cli*'s refreshing login file on the host."""
    path, content = {
        "claude": (home / ".claude/.credentials.json", _CLAUDE_LOGIN),
        "codex": (home / ".codex/auth.json", _CODEX_LOGIN),
    }[cli]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _findings(tmp_path, models=None, **agent) -> list[DiagnosticFinding]:
    """Run the whole Doctor on a Project whose ``[agent]`` is *agent*; return its findings.

    *models* is the Project's ``[models]`` table (Specialist -> model pins).
    """
    path = tmp_path / ".booley" / "booley.toml"
    document = tomlkit.parse(path.read_bytes().decode("utf-8"))
    document["agent"] = agent
    if models is not None:
        document["models"] = models
    path.write_bytes(tomlkit.dumps(document).encode("utf-8"))
    return list(run_doctor(tmp_path).findings)


_PIN = "agent.credential-policy"
_REINIT = "or re-run booley init"


def _pin_fails(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    return [f for f in findings if f.check_id == _PIN and f.severity is Severity.FAIL]


def _without_pin(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    return [f for f in findings if f.check_id != _PIN]


def _record_visitor(monkeypatch, cli: str) -> None:
    """Record the visitor mode the way a hand-written Sandbox does, so it is honoured."""
    pin_venue(monkeypatch, inside=True)
    monkeypatch.setenv("BOOLEY_AGENT_APP", cli)
    # Each login stands in for the host credential Booley seeds into the Sandbox. The seed
    # targets are fixed Sandbox paths that no public input redirects.
    for app, login in (("claude", ".claude/.credentials.json"), ("codex", ".codex/auth.json")):
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, app, str(Path.home() / login))


def _agent_fails(findings):
    """The FAILs of the agent credential checks; a whole run has FAILs of other sections too."""
    return [f for f in findings if f.severity is Severity.FAIL and "agent." in str(f.check_id)]


def _with_id(findings, check_id: str, subject: str) -> list[DiagnosticFinding]:
    return [f for f in findings if (f.check_id, f.subject) == (check_id, subject)]


@pytest.mark.parametrize(
    ("codex_auth", "models", "severity"),
    [
        ("subscription", None, Severity.WARN),
        ("api_key", None, Severity.WARN),
        # A non-default CLI that a [models] pin runs a Specialist on FAILs under every policy.
        ("subscription", {"reviewer": "gpt-5"}, Severity.FAIL),
        ("api_key", {"reviewer": "gpt-5"}, Severity.FAIL),
        # Guard: a pin on the default CLI leaves the other CLI at its WARN.
        ("subscription", {"reviewer": "claude-sonnet-5-5"}, Severity.WARN),
    ],
    ids=["subscription", "api-key", "pinned-subscription", "pinned-api-key", "pin-on-default"],
)
def test_other_allowed_cli_missing_credentials(home, tmp_path, codex_auth, models, severity):
    auth_token.store_token("sk-ant-oat01-x")
    auth = {"claude": "subscription", "codex": codex_auth}

    findings = _without_pin(
        _findings(tmp_path, models=models, clis=["claude", "codex"], default="claude", auth=auth)
    )

    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is severity
    assert auth_token.missing_credential_fix("codex", codex_auth) in missing.fix
    assert ("codex login" in missing.fix) == (codex_auth == "subscription")
    assert [f for f in _agent_fails(findings) if f is not missing] == []
    assert not _with_id(findings, "agent.rotation-risk", "codex")


@pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")
@pytest.mark.parametrize(
    ("models", "severity"), [({"reviewer": "gpt-5"}, Severity.FAIL), (None, Severity.WARN)]
)
def test_specialist_pinned_gateway_codex_grades_like_default(
    home, tmp_path, monkeypatch, models, severity
):
    """A pinned Codex fails on a token-command error as the default CLI does; unpinned it warns."""
    command, _ = write_token_command(tmp_path, [], stderr="no token yet\n", exit_code=1)
    pin_venue(monkeypatch, inside=True)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)
    auth_token.store_token("sk-ant-oat01-x")

    findings = _findings(tmp_path, models=models, clis=["claude", "codex"], default="claude")

    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is severity
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND exited 1" in missing.message


def test_default_cli_missing_credentials_fail(home, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    findings = _findings(
        tmp_path,
        clis=["claude", "codex"],
        default="claude",
        auth={"claude": "subscription", "codex": "api_key"},
    )

    (missing,) = _with_id(findings, "agent.credential-missing", "claude")
    assert missing.severity is Severity.FAIL
    assert any(f.message.startswith("Codex auth:") for f in findings)


@pytest.mark.parametrize(
    ("auth", "severity", "policy"),
    [
        ("subscription", Severity.FAIL, "subscription"),
        (None, Severity.FAIL, "subscription"),
        (auth_token.VISITOR_AUTH, Severity.WARN, auth_token.VISITOR_AUTH),
    ],
)
def test_default_cli_missing_credential_carries_login_fix(
    home, tmp_path, monkeypatch, auth, severity, policy
):
    if auth == auth_token.VISITOR_AUTH:
        _record_visitor(monkeypatch, "codex")
    agent = {"clis": ["codex"], "default": "codex", **({"auth": auth} if auth else {})}
    findings = _findings(tmp_path, **agent)

    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is severity
    assert missing.fix == auth_token.missing_credential_fix("codex", policy)
    assert missing.fix.startswith("codex login")


_EXPLICIT = "(accepted by explicit auth = 'subscription')"
_FALLBACK = "(the fallback policy auth = 'subscription' applies)"


@pytest.mark.parametrize(
    ("auth", "suffix"),
    [("subscription", _EXPLICIT), (None, _FALLBACK), ("auto", _FALLBACK), ("gateway", _FALLBACK)],
)
def test_rotation_note_names_recorded_or_fallback_policy(home, tmp_path, auth, suffix):
    _login(home, "codex")
    auth = {"codex": auth} if auth == "gateway" else auth
    agent = {"clis": ["codex"], "default": "codex", **({"auth": auth} if auth else {})}
    findings = _findings(tmp_path, **agent)

    (note,) = [f for f in findings if f.message.startswith("Codex: no rotation-free credential")]
    assert note.severity is Severity.NOTE
    assert note.message.endswith(suffix)
    assert ("explicit" in note.message) == (suffix == _EXPLICIT)


def test_rotation_risk_ids_keep_per_cli_subjects(home, tmp_path, monkeypatch):
    # An explicit ``subscription`` accepts the risk as a NOTE; the visitor mode warns.
    _record_visitor(monkeypatch, "claude")
    _login(home, "claude")
    _login(home, "codex")
    findings = _findings(
        tmp_path, clis=["claude", "codex"], default="claude", auth=auth_token.VISITOR_AUTH
    )

    for cli in ("claude", "codex"):
        (risk,) = _with_id(findings, "agent.rotation-risk", cli)
        assert risk.severity is Severity.WARN
        assert f"booley auth --cli {cli}" in risk.fix


_RISK = "{label}: no rotation-free credential; agents use a refreshing one, which a host-side login can revoke mid-run"


def test_single_cli_project_output_is_unchanged(home, tmp_path, monkeypatch):
    """The other CLI's login is present and installed, yet it is never audited."""
    _login(home, "claude")
    _login(home, "codex")
    for only, label, login in (
        ("claude", "Claude Code", home / ".claude/.credentials.json"),
        ("codex", "Codex", home / ".codex/auth.json"),
    ):
        _record_visitor(monkeypatch, only)
        findings = _without_pin(
            _findings(tmp_path, clis=[only], default=only, auth=auth_token.VISITOR_AUTH)
        )

        # A whole run holds every section's rows: keep the CLI's own, which are its credential
        # PASS and its rotation risk. The login-expiry line (Claude only) carries a
        # host-timezone date.
        rows = [
            f
            for f in findings
            if f.message.startswith(f"{label} auth:") or f.check_id == "agent.rotation-risk"
        ]
        assert [(f.severity.name, f.check_id, f.message, f.fix) for f in rows] == [
            ("PASS", None, f"{label} auth: {login}", ""),
            (
                "WARN",
                "agent.rotation-risk",
                _RISK.format(label=label),
                f"booley auth --cli {only}",
            ),
        ]
        agent_rows = [f for f in findings if (f.check_id or "").startswith("agent.")]
        assert {f.subject for f in agent_rows} <= {None, only}


_RETIRED = "auth 'auto' is retired"
_CLAUDE = {"clis": ["claude", "codex"], "default": "claude"}
_NOT_SET = "auth is not set"
_TABLE = "with an [agent.auth] table that "


@pytest.mark.parametrize(
    ("agent", "logins", "env", "expected"),
    [
        (
            {"clis": ["claude"], "default": "claude"},
            [],
            {"ANTHROPIC_API_KEY": "sk-ant-api03-x"},
            [("claude", _NOT_SET, "api_key", 'set auth = "api_key" under [agent]')],
        ),
        (
            {**_CLAUDE, "auth": "auto"},
            ["claude"],
            {"OPENAI_API_KEY": "sk-test"},
            [
                ("claude", _RETIRED, "subscription", _TABLE + 'sets claude = "subscription"'),
                ("codex", _RETIRED, "api_key", _TABLE + 'sets codex = "api_key"'),
            ],
        ),
        (
            {**_CLAUDE, "auth": {"claude": "subscription"}},
            ["claude"],
            {"OPENAI_API_KEY": "sk-test"},
            [("codex", _NOT_SET, "api_key", 'set codex = "api_key" under [agent.auth]')],
        ),
        (
            {"clis": ["codex"], "default": "codex", "auth": {"codex": "auto"}},
            ["codex"],
            {},
            [("codex", _RETIRED, "subscription", 'set codex = "subscription" under [agent.auth]')],
        ),
    ],
    ids=["missing-string", "auto-string", "missing-table", "auto-table"],
)
def test_unrecorded_auth_fails_with_detected_value(
    home, tmp_path, monkeypatch, agent, logins, env, expected
):
    for cli in logins:
        _login(home, cli)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    fails = _pin_fails(_findings(tmp_path, **agent))

    assert [(f.subject, f.message) for f in fails] == [
        (cli, f"{cli}: booley.toml [agent] {state}; detected: {value}")
        for cli, state, value, _fragment in expected
    ]
    for fail, (_cli, _state, _value, fragment) in zip(fails, expected, strict=True):
        assert fragment in fail.fix
        assert fail.fix.endswith(_REINIT)


def test_recorded_subscription_passes_with_an_exported_api_key(home, tmp_path, monkeypatch):
    _login(home, "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

    recorded = _findings(tmp_path, clis=["claude"], default="claude", auth="subscription")
    unrecorded = _findings(tmp_path, clis=["claude"], default="claude")

    assert _agent_fails(recorded) == []
    assert [f.message.rsplit("; ", 1)[-1] for f in _pin_fails(unrecorded)] == [
        "detected: subscription"
    ]


_VISITOR_STATE = "'visitor' applies only in a Sandbox set up by hand"


@pytest.mark.parametrize(
    ("inside", "env", "auth", "failed", "state"),
    [
        (True, {}, {}, ["codex", "claude"], "is not set"),
        (True, {"BOOLEY_PRIMARY_AUTH": "api_key"}, {}, ["codex", "claude"], "is not set"),
        (True, {}, {"auth": "visitor"}, [], ""),
        (
            True,
            {"BOOLEY_AGENT_CLIS": "codex=subscription,claude=subscription"},
            {"auth": "visitor"},
            ["codex", "claude"],
            _VISITOR_STATE,
        ),
        (False, {}, {"auth": "visitor"}, ["codex", "claude"], _VISITOR_STATE),
    ],
    ids=[
        "hand-written-missing",
        "hand-written-api-key",
        "hand-written-visitor",
        "issued-visitor",
        "host-visitor",
    ],
)
def test_auth_check_accepts_visitor_only_in_a_hand_written_sandbox(
    home, tmp_path, monkeypatch, inside, env, auth, failed, state
):
    """Only a recorded ``visitor`` in a hand-written Sandbox passes; a missing ``auth`` fails."""
    pin_venue(monkeypatch, inside=inside)
    for name, value in {"BOOLEY_AGENT_APP": "codex", "BOOLEY_AGENT_CLIS": "", **env}.items():
        monkeypatch.setenv(name, value)

    findings = _findings(tmp_path, clis=["codex", "claude"], default="codex", **auth)

    assert [f.message for f in _pin_fails(findings)] == [
        f"{cli}: booley.toml [agent] auth {state}; detected: subscription" for cli in failed
    ]
    assert not [f for f in findings if "visitor mode" in f.message]


_BOTH = ["claude", "codex"]
_CODEX_GATEWAY = {"clis": ["codex"], "auth": {"codex": "gateway"}}


@pytest.mark.parametrize(
    ("agent", "inside", "handoff", "codex_policy"),
    [
        pytest.param({"default": "codex"}, False, None, "subscription", id="declared-default"),
        pytest.param({"clis": _BOTH, "default": "codex"}, False, None, "subscription", id="clis"),
        pytest.param(
            {"clis": _BOTH, "auth": {"claude": "api_key", "codex": "api_key"}},
            False,
            None,
            "api_key",
            id="auth-table",
        ),
        pytest.param(_CODEX_GATEWAY, False, None, "subscription", id="gateway-on-host"),
        pytest.param(_CODEX_GATEWAY, True, None, "gateway", id="gateway-in-sandbox"),
        pytest.param({"clis": _BOTH}, True, "gateway", "gateway", id="gateway-handoff"),
    ],
)
def test_doctor_audits_what_the_runtime_resolves(
    tmp_path, monkeypatch, agent, inside, handoff, codex_policy
):
    from booley.config.agent import settings_from_data
    from booley.harness import doctor

    for name in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_AGENT_APP", "BOOLEY_AGENT_CLIS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    if handoff:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", handoff)
    pin_venue(monkeypatch, inside=inside)
    project = doctor.ProjectAudit(tmp_path, tmp_path / ".booley", {"agent": agent}, "")

    # private: a whole run shows the audit only through each CLI's rows.
    audit = doctor._agent_audit(project, None)

    runtime = settings_from_data({"agent": agent}, lazy=False, keep_retired_auth=True)
    assert (
        audit.default_cli,
        sorted(audit.allowed),
        {c: audit.policy(c) for c in audit.allowed},
    ) == (runtime.default_cli, sorted(runtime.clis), dict(runtime.auth))
    assert audit.allowed[0] == runtime.default_cli
    if "codex" in audit.allowed:
        assert audit.policy("codex") == codex_policy


def _project(tmp_path, agent):
    from booley.harness import doctor

    return doctor.ProjectAudit(tmp_path, tmp_path / ".booley", {"agent": agent}, "")


def test_auto_narrowing_keeps_a_listed_codex_from_gatewaying_claude(tmp_path, monkeypatch):
    from booley.config.agent_app import AgentAppChoice
    from booley.harness import doctor

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    pin_venue(monkeypatch, inside=True)
    agent = {"clis": _BOTH, "auth": {"claude": "subscription", "codex": "gateway"}}
    choice = AgentAppChoice("claude", None, "only Claude is logged in", ("claude",))

    audit = doctor._agent_audit(_project(tmp_path, agent), choice)

    assert audit.allowed == ("claude",)
    assert audit.policy("claude") == "subscription"


def test_auto_choice_does_not_undo_a_refused_agent_table(tmp_path, monkeypatch):
    from booley.config.agent_app import AgentAppChoice
    from booley.harness import doctor

    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    pin_venue(monkeypatch, inside=False)
    agent = {"default": "claude", "clis": ["codex"], "auth": "api_key"}
    choice = AgentAppChoice("claude", None, "both logged in", tuple(_BOTH))

    audit = doctor._agent_audit(_project(tmp_path, agent), choice)

    assert audit.policy("claude") == "subscription"
