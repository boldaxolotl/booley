"""In-Sandbox Doctor warns when Git would not commit as ``[agent.git]`` (#2122).

Real ``git`` runs with ``GIT_CONFIG_SYSTEM`` / ``GIT_CONFIG_GLOBAL`` pointed at temp files
(a GitHub Codespaces gitconfig layering), and the Sandbox's ``GIT_CONFIG_COUNT`` override is
set (or not) in the environment.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.harness import sandbox_git_identity
from booley.harness.sandbox_git_identity import IdentityCheck
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS
from tests.harness.doctor_host_support import serve_healthy_doctor_host
from tests.harness.doctor_run_support import enter_sandbox, findings, run_doctor, write_project
from tests.subprocess_budget import run_bounded_child

OVERRIDE_ID = "sandbox.git-identity-override"
GITCONFIG_ID = "sandbox.gitconfig-identity"
CREDENTIAL_ID = "sandbox.git-credential-helper"
AGENT_NAME = "Agent Bot"
AGENT_EMAIL = "agent@example.com"


class _SandboxGit:
    """A temp checkout plus fake system/global gitconfig for one test."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        run_bounded_child(["git", "init", "-q", str(self.checkout)], check=True)
        self.booley_dir = write_project(self.checkout)
        agent_git = f'\n[agent.git]\nname = "{AGENT_NAME}"\nemail = "{AGENT_EMAIL}"\n'
        with (self.booley_dir / "booley.toml").open("ab") as stream:
            stream.write(agent_git.encode())
        self.config_files = {scope: root / f"{scope}.gitconfig" for scope in ("system", "global")}
        for scope, path in self.config_files.items():
            path.write_bytes(b"")
            monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    def write_identity(self, scope: str, name: str, email: str) -> None:
        """Make the *scope* ("system" or "global") gitconfig carry a user identity."""
        self.config_files[scope].write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")

    def add_credential_helper(self, scope: str, helper: str, url: str | None = None) -> None:
        """Append a ``credential[.<url>].helper`` line through Git, so Git does the quoting."""
        key = f"credential.{url}.helper" if url else "credential.helper"
        file = str(self.config_files[scope])
        run_bounded_child(["git", "config", "--file", file, "--add", key, helper], check=True)

    def set_override(self, *pairs: tuple[str, str]) -> None:
        """Set the ``GIT_CONFIG_COUNT`` override to the given key/value pairs."""
        self.monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(pairs)))
        for index, (key, value) in enumerate(pairs):
            self.monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
            self.monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)

    def set_full_override(self) -> None:
        """Set the override exactly as Booley issues it for ``[agent.git]``."""
        self.set_override(("user.name", AGENT_NAME), ("user.email", AGENT_EMAIL))


@pytest.fixture
def sandbox_git(tmp_path, monkeypatch) -> _SandboxGit:
    """Isolate every ambient Git and Booley variable from the host running the suite."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    ambient = ("GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "CLAUDECODE")
    ambient += ("GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES")
    for index in range(8):
        ambient += (f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}")
    for name in ambient:
        monkeypatch.delenv(name, raising=False)
    return _SandboxGit(tmp_path, monkeypatch)


def _inspect(sandbox: _SandboxGit) -> dict[str, IdentityCheck]:
    """What the three public inspections report for the checkout, by check ID."""
    checkout = sandbox.checkout
    return {
        OVERRIDE_ID: sandbox_git_identity.inspect_override(checkout, sandbox.booley_dir),
        GITCONFIG_ID: sandbox_git_identity.inspect_gitconfig_identity(checkout),
        CREDENTIAL_ID: sandbox_git_identity.inspect_credential_helpers(checkout),
    }


def _warned(checks: dict[str, IdentityCheck], check_id: str) -> IdentityCheck:
    """The check for *check_id*, which must carry a warning of that ID."""
    check = checks[check_id]
    assert check.warning is not None, f"expected a {check_id} warning, got {check.passed!r}"
    assert (check.warning.check_id, check.warning.subject) == (check_id, None)
    return check


def _passed(checks: dict[str, IdentityCheck], check_id: str) -> str:
    assert checks[check_id].warning is None, checks[check_id].warning
    return checks[check_id].passed


_VISITOR = ("Visitor", "v@example.com")
_RECREATE = (
    f"recreate the Sandbox so it picks up the current [agent.git] ({UNKNOWN_SANDBOX_STEPS})"
)


_NAME_ONLY = ({"system": ("Visitor", "visitor@example.com")}, [("user.name", AGENT_NAME)])
_OTHER_NAME = ({}, [("user.name", "Someone Else"), ("user.email", AGENT_EMAIL)])


@pytest.mark.parametrize(
    ("identities", "pairs", "toml", "has", "lacks"),
    [
        ({"global": _VISITOR}, [], b"", "git uses 'Visitor' from global config", ""),
        ({}, "bogus", b"", "bogus count in GIT_CONFIG_COUNT", ""),
        (*_NAME_ONLY, b"", "git uses 'visitor@example.com' from", "user.name is not set"),
        (*_OTHER_NAME, b"", f"'Someone Else', [agent.git] expects '{AGENT_NAME}'", ""),
        ({}, [], b"", "git has no value", ""),
        ({}, [], b"[agent.git]\nname = 3\n", "[agent.git] is invalid: ", ""),
    ],
    ids=["missing", "malformed", "name-only", "different", "nothing", "invalid-agent-git"],
)
def test_override_warning_names_the_problem(sandbox_git, identities, pairs, toml, has, lacks):
    for scope, identity in identities.items():
        sandbox_git.write_identity(scope, *identity)
    if pairs == "bogus":
        sandbox_git.monkeypatch.setenv("GIT_CONFIG_COUNT", pairs)
    elif pairs:
        sandbox_git.set_override(*pairs)
    if toml:
        (sandbox_git.booley_dir / "booley.toml").write_bytes(toml)

    check = _warned(_inspect(sandbox_git), OVERRIDE_ID)

    assert has in check.warning
    assert not lacks or lacks not in check.warning
    if not toml:
        assert _RECREATE in check.fix
        assert "refresh and recreate" not in check.fix


_DEV = ("Dev", "dev@localhost")


@pytest.mark.parametrize(
    ("system", "global_", "foreign"),
    [(_VISITOR, None, True), (None, _VISITOR, True), (_DEV, None, False), (_DEV, _VISITOR, True)],
    ids=["system", "global", "image-default", "foreign-beside-default"],
)
def test_gitconfig_identity_is_foreign_unless_it_is_the_image_default(
    sandbox_git, system, global_, foreign
):
    # The Sandbox Image writes the default pair into /etc/gitconfig (Dockerfile.base).
    for scope, identity in (("system", system), ("global", global_)):
        if identity:
            sandbox_git.write_identity(scope, *identity)
    sandbox_git.set_full_override()

    checks = _inspect(sandbox_git)

    # The override is complete, so the two checks are independent.
    assert _passed(checks, OVERRIDE_ID).startswith("Git identity override matches")
    if foreign:
        message = _warned(checks, GITCONFIG_ID).warning
        assert "Visitor" in message
        assert "'Dev'" not in message
    else:
        assert "set no identity other than Dev <dev@localhost>" in _passed(checks, GITCONFIG_ID)


_GH = "!f() {\n /usr/bin/gh auth git-credential; }; f"


@pytest.mark.parametrize(
    ("scope", "helper", "url", "shown"),
    [
        ("system", "store", None, "store"),
        ("global", _GH, "https://github.com", "/usr/bin/gh auth git-credential"),
    ],
    ids=["system-store", "global-gh"],
)
def test_credential_helper_warns(sandbox_git, scope, helper, url, shown):
    sandbox_git.add_credential_helper(scope, helper, url)
    sandbox_git.set_full_override()

    check = _warned(_inspect(sandbox_git), CREDENTIAL_ID)

    assert shown in check.warning
    # Git echoes the path as given for some scopes and slash-normalized for others.
    assert sandbox_git.config_files[scope].as_posix() in check.warning.replace("\\", "/")
    assert "editor attach" in check.warning
    assert "dev.containers.gitCredentialHelperConfigLocation" in check.fix


@pytest.mark.parametrize(
    "config",
    [b"[credential]\n\thelper =\n", b"[credential]\n\thelper\n"],
    ids=["reset", "no-value"],
)
def test_credential_helper_without_a_value_does_not_warn(sandbox_git, config):
    sandbox_git.config_files["global"].write_bytes(config)
    sandbox_git.set_full_override()

    assert _passed(_inspect(sandbox_git), CREDENTIAL_ID) == (
        "system and global gitconfig set no Git credential helper"
    )


def test_empty_global_helper_resets_system_helpers_only(sandbox_git):
    for scope, helper in (("system", "store"), ("global", ""), ("global", "cache")):
        sandbox_git.add_credential_helper(scope, helper)
    sandbox_git.set_full_override()

    message = _warned(_inspect(sandbox_git), CREDENTIAL_ID).warning

    assert "'cache'" in message
    assert "'store'" not in message


def test_credential_helper_is_listed_not_run(sandbox_git, tmp_path):
    marker = tmp_path / "helper-ran"
    script = tmp_path / "helper.sh"
    script.write_bytes(f"touch {marker.as_posix()}\necho password=SECRET1\n".encode())
    global_file = sandbox_git.config_files["global"]
    add = sandbox_git.add_credential_helper
    add("global", f"!sh {script.as_posix()}")
    add("global", "store", "https://user:SECRET2@example.com")
    add("global", "!f() { echo password=SECRET3; }; f")
    add("global", "!f() { echo 'password=SECRET4 SECRET5'; }; f")
    add("global", "store", "https://user:SEC@RET6@example.com")
    before = global_file.read_bytes()
    sandbox_git.set_full_override()

    checks = _inspect(sandbox_git)

    assert script.as_posix() in _warned(checks, CREDENTIAL_ID).warning
    assert not marker.exists()
    everything = "".join(c.passed + str(c.warning) + c.fix for c in checks.values())
    for secret in ("SECRET1", "SECRET2", "SECRET3", "SECRET4", "SECRET5", "RET6"):
        assert secret not in everything
    assert global_file.read_bytes() == before


def test_unrunnable_git_warns_for_every_check(sandbox_git, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-git"))

    checks = _inspect(sandbox_git)

    for check_id in (OVERRIDE_ID, GITCONFIG_ID, CREDENTIAL_ID):
        assert "could not be inspected" in _warned(checks, check_id).warning


@pytest.mark.parametrize("waived", [None, OVERRIDE_ID, GITCONFIG_ID, CREDENTIAL_ID])
def test_doctor_reports_each_git_check_and_honours_its_waiver(
    sandbox_git, monkeypatch, capsys, waived
):
    """Doctor presents the three checks, and a waiver of one (#2122) leaves the others WARN."""
    serve_healthy_doctor_host(monkeypatch, sandbox_git.checkout)
    enter_sandbox(monkeypatch)
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    if waived is None:
        sandbox_git.set_full_override()
    else:
        sandbox_git.write_identity("global", *_VISITOR)
        sandbox_git.add_credential_helper("global", "store")
        waiver = (
            f'version = 1\n[[waiver]]\ncheck = "{waived}"\nreason = "#2122"\npermanent = true\n'
        )
        (sandbox_git.booley_dir / "doctor-waivers.toml").write_bytes(waiver.encode())

    result = run_doctor(sandbox_git.checkout)

    out = capsys.readouterr().out
    severities = {
        c: [f.severity for f in findings(result, c)]
        for c in (OVERRIDE_ID, GITCONFIG_ID, CREDENTIAL_ID)
    }
    if waived is None:
        assert not any(severities.values())
        passes = ("override matches [agent.git]", "other than Dev <dev@localhost>")
        assert all(text in out for text in (*passes, "no Git credential helper"))
        return
    assert severities == {
        c: [Severity.WAIVED if c == waived else Severity.WARN] for c in severities
    }
    assert all(c in out for c in severities if c != waived)
