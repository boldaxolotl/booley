"""In-Sandbox Doctor warns when Git would not commit as ``[agent.git]`` (#2122).

The tests fake a GitHub Codespaces gitconfig layering: real ``git`` runs with
``GIT_CONFIG_SYSTEM`` / ``GIT_CONFIG_GLOBAL`` pointed at temp files, and the
Sandbox's ``GIT_CONFIG_COUNT`` override is set (or not) in the environment.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.audit.doctor_waivers import load_doctor_waivers
from booley.harness import activity_hooks, doctor, web_isolation
from booley.runtime import dashboard_extension, runtime_context

OVERRIDE_ID = "sandbox.git-identity-override"
GITCONFIG_ID = "sandbox.gitconfig-identity"
AGENT_NAME = "Agent Bot"
AGENT_EMAIL = "agent@example.com"


class _SandboxGit:
    """A temp checkout plus fake system/global gitconfig for one test."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        self.booley_dir = self.checkout / ".booley"
        self.booley_dir.mkdir()
        (self.booley_dir / "booley.toml").write_text(
            f'[agent.git]\nname = "{AGENT_NAME}"\nemail = "{AGENT_EMAIL}"\n'
        )
        self.config_files = {
            "system": root / "system.gitconfig",
            "global": root / "global.gitconfig",
        }
        for path in self.config_files.values():
            path.write_text("")
        xdg = root / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(self.config_files["system"]))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(self.config_files["global"]))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    def write_identity(self, scope: str, name: str, email: str) -> None:
        """Make the *scope* ("system" or "global") gitconfig carry a user identity."""
        self.config_files[scope].write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")

    def set_override(self, *pairs: tuple[str, str]) -> None:
        """Set the ``GIT_CONFIG_COUNT`` override to the given key/value pairs."""
        self.monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(pairs)))
        for index, (key, value) in enumerate(pairs):
            self.monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
            self.monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)

    def set_full_override(self) -> None:
        """Set the override exactly as Booley issues it for ``[agent.git]``."""
        self.set_override(("user.name", AGENT_NAME), ("user.email", AGENT_EMAIL))

    def audit(self) -> doctor.ProjectAudit:
        """The project handed to Doctor's container checks."""
        return doctor.ProjectAudit(
            project_root=self.checkout,
            booley_dir=self.booley_dir,
            booley_toml={},
            configs_toml={"x": {}},
            first_target="x",
        )


@pytest.fixture
def sandbox_git(tmp_path, monkeypatch) -> _SandboxGit:
    """Isolate every ambient Git and Booley variable from the host running the suite."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    for name in (
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CEILING_DIRECTORIES",
        "CLAUDECODE",
    ):
        monkeypatch.delenv(name, raising=False)
    for index in range(8):
        monkeypatch.delenv(f"GIT_CONFIG_KEY_{index}", raising=False)
        monkeypatch.delenv(f"GIT_CONFIG_VALUE_{index}", raising=False)
    return _SandboxGit(tmp_path, monkeypatch)


def _run_checks(sandbox: _SandboxGit, reporter: doctor._Reporter) -> None:
    """Drive Doctor's in-Sandbox container checks through the real reporter."""
    sandbox.monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    sandbox.monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    sandbox.monkeypatch.setattr(activity_hooks, "hooks_error", lambda: None)
    sandbox.monkeypatch.setattr(dashboard_extension, "installed_error", lambda: None)
    doctor._run_container_checks(
        sandbox.audit(),
        None,
        "booley-sandbox",
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )


def _warns(reporter: doctor._Reporter, check_id: str):
    return [f for f in reporter.findings if f.severity is Severity.WARN and f.check_id == check_id]


def _single_warn(reporter: doctor._Reporter, check_id: str):
    """The one WARN for *check_id*; fails by assertion when absent or repeated."""
    found = _warns(reporter, check_id)
    assert len(found) == 1, f"expected one {check_id} WARN, got {len(found)}"
    return found[0]


def test_missing_override_warns_with_check_id(sandbox_git, capsys):
    sandbox_git.write_identity("global", "Visitor", "v@example.com")
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    finding = _single_warn(reporter, OVERRIDE_ID)
    assert finding.subject is None
    assert "Visitor" in finding.message
    assert "global" in finding.message
    assert f"[{OVERRIDE_ID}]" in capsys.readouterr().out


def test_malformed_override_warns_with_git_error(sandbox_git):
    sandbox_git.monkeypatch.setenv("GIT_CONFIG_COUNT", "bogus")
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    finding = _single_warn(reporter, OVERRIDE_ID)
    assert "GIT_CONFIG_COUNT" in finding.message


def test_name_only_override_names_missing_email(sandbox_git):
    sandbox_git.write_identity("system", "Visitor", "visitor@example.com")
    sandbox_git.set_override(("user.name", AGENT_NAME))
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    finding = _single_warn(reporter, OVERRIDE_ID)
    assert "user.email" in finding.message
    assert "visitor@example.com" in finding.message
    assert "user.name is not set" not in finding.message


def test_different_override_warns(sandbox_git):
    sandbox_git.set_override(("user.name", "Someone Else"), ("user.email", AGENT_EMAIL))
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    finding = _single_warn(reporter, OVERRIDE_ID)
    assert "Someone Else" in finding.message
    assert AGENT_NAME in finding.message


def test_matching_override_and_clean_gitconfig_pass(sandbox_git, capsys):
    sandbox_git.set_full_override()
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    assert not _warns(reporter, OVERRIDE_ID)
    assert not _warns(reporter, GITCONFIG_ID)
    out = capsys.readouterr().out
    assert "Git identity override matches [agent.git]" in out
    assert "no user.name/user.email in system or global gitconfig" in out


@pytest.mark.parametrize("scope", ["system", "global"])
def test_gitconfig_identity_warns(sandbox_git, scope):
    sandbox_git.write_identity(scope, "Visitor", "v@example.com")
    sandbox_git.set_full_override()
    reporter = doctor._Reporter.create()
    _run_checks(sandbox_git, reporter)
    finding = _single_warn(reporter, GITCONFIG_ID)
    assert "Visitor" in finding.message
    # The override is complete, so the two checks are independent.
    assert not _warns(reporter, OVERRIDE_ID)


@pytest.mark.parametrize("check_id", [OVERRIDE_ID, GITCONFIG_ID])
def test_identity_warnings_are_waivable(sandbox_git, check_id):
    sandbox_git.write_identity("global", "Visitor", "v@example.com")
    (sandbox_git.booley_dir / "doctor-waivers.toml").write_text(
        "version = 1\n[[waiver]]\n"
        f'check = "{check_id}"\n'
        'reason = "Accepted for the #2122 regression test."\npermanent = true\n'
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(sandbox_git.booley_dir))
    _run_checks(sandbox_git, reporter)
    assert [f.check_id for f in reporter.findings if f.severity is Severity.WAIVED] == [check_id]
    assert not _warns(reporter, check_id)
