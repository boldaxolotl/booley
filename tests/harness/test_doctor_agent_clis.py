"""Doctor audits the credential of every Agent CLI a Project allows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
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


def _findings(tmp_path: Path, **agent) -> list[DiagnosticFinding]:
    """Run the agent credential checks for a Project whose ``[agent]`` is *agent*."""
    audit = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=tmp_path / ".booley",
        booley_toml={"agent": agent},
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    return list(reporter.findings)


def _with_id(findings, check_id: str, subject: str) -> list[DiagnosticFinding]:
    return [f for f in findings if (f.check_id, f.subject) == (check_id, subject)]


@pytest.mark.parametrize(
    "codex_auth,fix_has,fix_lacks",
    [("auto", "codex login", None), ("api_key", "booley auth --cli codex", "codex login")],
    ids=["auto", "api_key"],
)
def test_other_allowed_cli_missing_credentials_warn_with_login_fix(
    home, tmp_path, codex_auth, fix_has, fix_lacks
):
    auth_token.store_token("sk-ant-oat01-x")
    findings = _findings(
        tmp_path,
        clis=["claude", "codex"],
        default="claude",
        auth={"claude": "auto", "codex": codex_auth},
    )

    assert not [f for f in findings if f.severity is Severity.FAIL]
    (missing,) = _with_id(findings, "agent.credential-missing", "codex")
    assert missing.severity is Severity.WARN
    assert fix_has in missing.fix
    assert fix_lacks is None or fix_lacks not in missing.fix
    assert not _with_id(findings, "agent.rotation-risk", "codex")


def test_default_cli_missing_credentials_fail(home, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    findings = _findings(
        tmp_path,
        clis=["claude", "codex"],
        default="claude",
        auth={"claude": "subscription", "codex": "auto"},
    )

    (missing,) = _with_id(findings, "agent.credential-missing", "claude")
    assert missing.severity is Severity.FAIL
    assert any(f.message.startswith("Codex auth:") for f in findings)


def test_rotation_risk_ids_keep_per_cli_subjects(home, tmp_path):
    _login(home, "claude")
    _login(home, "codex")
    findings = _findings(tmp_path, clis=["claude", "codex"], default="claude")

    for cli in ("claude", "codex"):
        (risk,) = _with_id(findings, "agent.rotation-risk", cli)
        assert risk.severity is Severity.WARN
        assert f"booley auth --cli {cli}" in risk.fix


_RISK = "{label}: no rotation-free credential; agents use a refreshing one, which a host-side login can revoke mid-run"


@pytest.mark.parametrize(
    "only,label", [("claude", "Claude Code"), ("codex", "Codex")], ids=["claude", "codex"]
)
def test_single_cli_project_output_is_unchanged(home, tmp_path, only, label):
    """The other CLI's login is present and installed, yet it is never audited."""
    _login(home, "claude")
    _login(home, "codex")
    findings = _findings(tmp_path, clis=[only], default=only)

    login = home / (".claude/.credentials.json" if only == "claude" else ".codex/auth.json")
    # The login-expiry line (Claude only) carries a host-timezone date; compare the rest exactly.
    rows = [f for f in findings if not f.message.startswith("subscription login valid until ")]
    assert [(f.severity.name, f.check_id, f.message, f.fix) for f in rows] == [
        ("PASS", None, f"{label} auth: {login}", ""),
        ("WARN", "agent.rotation-risk", _RISK.format(label=label), f"booley auth --cli {only}"),
    ]
    assert {f.subject for f in findings} <= {None, only}
