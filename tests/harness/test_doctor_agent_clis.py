"""Doctor audits the credential of every Agent CLI a Project allows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.config import agent as config_agent
from booley.harness import doctor
from booley.runtime import auth_token, runtime_context

_CLAUDE_LOGIN = {
    "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 4102444800000}
}
_CODEX_LOGIN = {"tokens": {"access_token": "a", "refresh_token": "r"}}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """An empty host: no credentials in the environment or on disk, both CLIs installed."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "CODEX_HOME",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_AGENT_APP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _cli: True)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    return home


def _login(home: Path, cli: str) -> None:
    """Leave *cli*'s refreshing login file on the host."""
    path, content = {
        "claude": (home / ".claude" / ".credentials.json", _CLAUDE_LOGIN),
        "codex": (home / ".codex" / "auth.json", _CODEX_LOGIN),
    }[cli]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _findings(
    tmp_path: Path, models: dict[str, str] | None = None, **agent
) -> list[DiagnosticFinding]:
    """Run the agent credential checks for a Project whose ``[agent]`` is *agent*.

    *models* is the Project's ``[models]`` table (Specialist -> model pins).
    """
    booley_toml: dict = {"agent": agent}
    if models is not None:
        booley_toml["models"] = models
    audit = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=tmp_path / ".booley",
        booley_toml=booley_toml,
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    return list(reporter.findings)


_PIN = "agent.credential-policy"
_REINIT = "or re-run booley init"


def _pin_fails(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    return [f for f in findings if f.check_id == _PIN and f.severity is Severity.FAIL]


def _without_pin(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    return [f for f in findings if f.check_id != _PIN]


def _visitor_policy(_project, cli=None) -> str:
    return auth_token.VISITOR_AUTH


def _with_id(findings, check_id: str, subject: str) -> list[DiagnosticFinding]:
    return [f for f in findings if (f.check_id, f.subject) == (check_id, subject)]


def test_other_allowed_cli_missing_credentials_warn_with_login_fix(home, tmp_path):
    auth_token.store_token("sk-ant-oat01-x")
    # Codex policy -> (text its fix must contain, text it must not contain)
    cases = {
        "subscription": ("codex login", None),
        "api_key": ("booley auth --cli codex", "codex login"),
    }
    for codex_auth, (fix_has, fix_lacks) in cases.items():
        findings = _without_pin(
            _findings(
                tmp_path,
                clis=["claude", "codex"],
                default="claude",
                auth={"claude": "subscription", "codex": codex_auth},
            )
        )

        assert not [f for f in findings if f.severity is Severity.FAIL]
        (missing,) = _with_id(findings, "agent.credential-missing", "codex")
        assert missing.severity is Severity.WARN
        assert fix_has in missing.fix
        assert fix_lacks is None or fix_lacks not in missing.fix
        assert not _with_id(findings, "agent.rotation-risk", "codex")


def test_specialist_pinned_cli_missing_credentials_fail(home, tmp_path):
    """A non-default CLI that a [models] pin runs a Specialist on FAILs under every policy."""
    auth_token.store_token("sk-ant-oat01-x")
    for codex_auth in ("subscription", "api_key"):
        findings = _without_pin(
            _findings(
                tmp_path,
                models={"reviewer": "gpt-5"},
                clis=["claude", "codex"],
                default="claude",
                auth={"claude": "subscription", "codex": codex_auth},
            )
        )

        (missing,) = _with_id(findings, "agent.credential-missing", "codex")
        assert missing.severity is Severity.FAIL
        assert auth_token.missing_credential_fix("codex", codex_auth) in missing.fix
        assert not [f for f in findings if f.severity is Severity.FAIL and f is not missing]
        assert not _with_id(findings, "agent.rotation-risk", "codex")

    # Guard: a pin on the default CLI leaves the other CLI at its WARN.
    findings = _without_pin(
        _findings(
            tmp_path,
            models={"reviewer": "claude-sonnet-5-5"},
            clis=["claude", "codex"],
            default="claude",
        )
    )
    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is Severity.WARN


def test_specialist_pinned_gateway_codex_grades_like_default(home, tmp_path, monkeypatch):
    """A pinned Codex fails on a token-command error as the default CLI does; unpinned it warns."""
    recorded: list[bool] = []
    monkeypatch.setattr(
        doctor,
        "_configured_auth_policy",
        lambda _project, cli=None: "gateway" if cli == "codex" else "subscription",
    )
    monkeypatch.setattr(
        doctor,
        "_check_gateway_auth",
        lambda _cli, _reporter, *, chosen_by_auto: recorded.append(chosen_by_auto),
    )
    auth_token.store_token("sk-ant-oat01-x")
    agent = {"clis": ["claude", "codex"], "default": "claude"}

    _findings(tmp_path, models={"reviewer": "gpt-5"}, **agent)
    assert recorded == [False]

    recorded.clear()
    _findings(tmp_path, **agent)
    assert recorded == [True]


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
    ("auth", "visitor", "severity", "policy"),
    [
        ("subscription", False, Severity.FAIL, "subscription"),
        (None, False, Severity.FAIL, "subscription"),
        (None, True, Severity.WARN, auth_token.VISITOR_AUTH),
    ],
)
def test_default_cli_missing_credential_carries_login_fix(
    home, tmp_path, monkeypatch, auth, visitor, severity, policy
):
    if visitor:
        monkeypatch.setattr(doctor, "_configured_auth_policy", _visitor_policy)
    agent = {"clis": ["codex"], "default": "codex"}
    if auth:
        agent["auth"] = auth
    findings = _findings(tmp_path, **agent)

    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is severity
    assert missing.fix == auth_token.missing_credential_fix("codex", policy)
    assert missing.fix.startswith("codex login")


_EXPLICIT = "(accepted by explicit auth = 'subscription')"
_FALLBACK = "(the fallback policy auth = 'subscription' applies)"


@pytest.mark.parametrize(
    ("auth", "suffix"),
    [
        ("subscription", _EXPLICIT),
        (None, _FALLBACK),
        ("auto", _FALLBACK),
        ({"codex": "gateway"}, _FALLBACK),
    ],
)
def test_rotation_note_names_recorded_or_fallback_policy(home, tmp_path, auth, suffix):
    _login(home, "codex")
    agent = {"clis": ["codex"], "default": "codex"}
    if auth:
        agent["auth"] = auth
    findings = _findings(tmp_path, **agent)

    (note,) = [f for f in findings if f.message.startswith("Codex: no rotation-free credential")]
    assert note.severity is Severity.NOTE
    assert note.message.endswith(suffix)
    assert ("explicit" in note.message) == (suffix == _EXPLICIT)


def test_rotation_risk_ids_keep_per_cli_subjects(home, tmp_path, monkeypatch):
    # An explicit ``subscription`` accepts the risk as a NOTE; the visitor mode warns.
    monkeypatch.setattr(doctor, "_configured_auth_policy", _visitor_policy)
    _login(home, "claude")
    _login(home, "codex")
    findings = _findings(tmp_path, clis=["claude", "codex"], default="claude")

    for cli in ("claude", "codex"):
        (risk,) = _with_id(findings, "agent.rotation-risk", cli)
        assert risk.severity is Severity.WARN
        assert f"booley auth --cli {cli}" in risk.fix


_RISK = "{label}: no rotation-free credential; agents use a refreshing one, which a host-side login can revoke mid-run"


def test_single_cli_project_output_is_unchanged(home, tmp_path, monkeypatch):
    """The other CLI's login is present and installed, yet it is never audited."""
    monkeypatch.setattr(doctor, "_configured_auth_policy", _visitor_policy)
    _login(home, "claude")
    _login(home, "codex")
    for only, label, login in (
        ("claude", "Claude Code", home / ".claude/.credentials.json"),
        ("codex", "Codex", home / ".codex/auth.json"),
    ):
        findings = _without_pin(_findings(tmp_path, clis=[only], default=only))

        # The login-expiry line (Claude only) carries a host-timezone date; compare the rest.
        rows = [f for f in findings if not f.message.startswith("subscription login valid until ")]
        assert [(f.severity.name, f.check_id, f.message, f.fix) for f in rows] == [
            ("PASS", None, f"{label} auth: {login}", ""),
            (
                "WARN",
                "agent.rotation-risk",
                _RISK.format(label=label),
                f"booley auth --cli {only}",
            ),
        ]
        assert {f.subject for f in findings} <= {None, only}


@pytest.mark.parametrize(
    ("agent", "logins", "env", "expected"),
    [
        (
            {"clis": ["claude"], "default": "claude"},
            [],
            {"ANTHROPIC_API_KEY": "sk-ant-api03-x"},
            [("claude", "auth is not set", "api_key", 'set auth = "api_key" under [agent]')],
        ),
        (
            {"clis": ["claude", "codex"], "default": "claude", "auth": "auto"},
            ["claude"],
            {"OPENAI_API_KEY": "sk-test"},
            [
                (
                    "claude",
                    "auth 'auto' is retired",
                    "subscription",
                    'with an [agent.auth] table that sets claude = "subscription"',
                ),
                (
                    "codex",
                    "auth 'auto' is retired",
                    "api_key",
                    'with an [agent.auth] table that sets codex = "api_key"',
                ),
            ],
        ),
        (
            {"clis": ["claude", "codex"], "default": "claude", "auth": {"claude": "subscription"}},
            ["claude"],
            {"OPENAI_API_KEY": "sk-test"},
            [("codex", "auth is not set", "api_key", 'set codex = "api_key" under [agent.auth]')],
        ),
        (
            {"clis": ["codex"], "default": "codex", "auth": {"codex": "auto"}},
            ["codex"],
            {},
            [
                (
                    "codex",
                    "auth 'auto' is retired",
                    "subscription",
                    'set codex = "subscription" under [agent.auth]',
                )
            ],
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
        (
            cli,
            f"{cli}: booley.toml [agent] {state}; detected: {value}",
        )
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

    assert not [f for f in recorded if f.severity is Severity.FAIL]
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
    monkeypatch.setattr(config_agent, "_inside_container", lambda: inside)
    for name, value in {"BOOLEY_AGENT_APP": "codex", "BOOLEY_AGENT_CLIS": "", **env}.items():
        monkeypatch.setenv(name, value)

    findings = _findings(tmp_path, clis=["codex", "claude"], default="codex", **auth)

    assert [f.message for f in _pin_fails(findings)] == [
        f"{cli}: booley.toml [agent] auth {state}; detected: subscription" for cli in failed
    ]
    assert not [f for f in findings if "visitor mode" in f.message]
