"""Project Initialization runs anywhere: idempotent, with no Docker or host-private state."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.fusesoc.core_projection import PROJECTED_CORE_GLOB
from booley.harness import init_cmd
from booley.harness.setup import readiness
from booley.harness.setup.common import InitContext, StepResult
from booley.runtime import auth_token
from booley.runtime.booley_dir import init_booley_dir_scope, reset_cache
from tests.docker_cli_guard import assert_docker_cli_unused, forbid_docker_cli
from tests.harness.git_support import git_stdout as _git
from tests.subprocess_budget import run_bounded_child

CLAUDE = init_cmd.AgentSelection("claude", "subscription")
CORE_TEXT = "CAPI=2:\nname: example:lib:cpu:1.0\n"
HOST_PRIVATE_ENVIRONMENT = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repo with a Booley guide and a Stealth core, host-private state absent."""
    root = tmp_path / "proj"
    root.mkdir()
    run_bounded_child(["git", "init", str(root)], check=True)
    (root / ".booley" / "cores").mkdir(parents=True)
    (root / ".booley" / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8")
    (root / ".booley" / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
    (root / ".booley" / "cores" / "cpu.core").write_text(CORE_TEXT, encoding="utf-8")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    # No docker CLI, and every host-private location points at a path that does not exist.
    forbid_docker_cli(monkeypatch)
    monkeypatch.setattr(init_cmd.shutil, "which", lambda _name: None)
    for name in HOST_PRIVATE_ENVIRONMENT:
        monkeypatch.setenv(name, str(tmp_path / "absent" / name))
    for name in (*auth_token.CREDENTIAL_ENV_KEYS, "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    reset_cache()
    yield root
    assert_docker_cli_unused()


def _initialize(
    root: Path, *, check_only: bool = False, agent: init_cmd.AgentSelection | None = None
) -> list[StepResult]:
    """One Project Initialization run with fixed Sandbox Git facts; returns its records."""
    ctx = InitContext(
        project_root=root, interactive=False, show_step_banners=False, check_only=check_only
    )
    config = root / ".booley" / "booley.toml"
    request = init_cmd.ProjectInitialization(
        agent, config, lambda: init_cmd.ProjectFacts((2, 50, 0))
    )
    with init_booley_dir_scope(root):
        init_cmd.initialize_project(ctx, request)
    return ctx.results


def _repository_state(root: Path) -> dict[str, object]:
    """Files with modes and local git config of both repositories; indexes excluded."""
    files = {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode)
        for path in root.rglob("*")
        if path.is_file() and not (path.name == "index" and path.parent.name == ".git")
    }
    configs = {
        repository: run_bounded_child(
            ["git", "-C", str(root / repository), "config", "--local", "--list"],
            check=True,
        ).stdout
        for repository in (".", ".booley")
        if (root / repository / ".git").exists()
    }
    return {"files": files, "git_config": configs}


def test_second_project_initialization_changes_nothing(project: Path) -> None:
    first = _initialize(project, agent=CLAUDE)
    after_first = _repository_state(project)
    second = _initialize(project, agent=CLAUDE)

    assert any(result.status == "ok" for result in first)  # the first run really did work
    assert _repository_state(project) == after_first
    # Steady-state reports: these steps record `ok` even when they find nothing to do.
    assert [(result.name, result.status) for result in second if result.status != "current"] == [
        ("agent_config", "skip"),
        ("core_projections", "ok"),
        ("git_hooks", "skip"),
        ("line_endings", "ok"),
        ("guidance_links", "ok"),
    ]
    assert [result.detail for result in second if result.name == "core_projections"] == [
        "0 change(s)"
    ]


def test_project_initialization_runs_without_docker_or_host_private_state(
    project: Path, tmp_path: Path
) -> None:
    audit = readiness.ProjectAudit(
        project, project / ".booley", {"stealth": {"enabled": True}}, {}, ""
    )

    def readiness_findings(*, ignore_exclude: bool) -> list[tuple[str | None, str]]:
        reports = (readiness.check_guidance(audit), readiness.check_stealth_cores(audit))
        return [
            (finding.check_id, finding.fix)
            for report in reports
            for finding in report.findings
            if finding.severity in {Severity.WARN, Severity.FAIL}
            and not (ignore_exclude and "not excluded" in finding.message)
        ]

    before = readiness_findings(ignore_exclude=True)
    assert [check_id for check_id, _fix in before] == [
        "guidance.links-unhealthy",
        "stealth.projections-not-current",
    ]
    assert all("booley init" in fix for _check_id, fix in before)

    results = _initialize(project, agent=CLAUDE)

    assert (
        readiness_findings(ignore_exclude=False) == []
    )  # the named command cleared both findings
    assert [result for result in results if result.status == "err"] == []
    assert len(list(project.glob(PROJECTED_CORE_GLOB))) == 1  # the Stealth core was projected
    assert (project / "AGENTS.md").exists()  # the guidance link was created
    for name in HOST_PRIVATE_ENVIRONMENT:
        assert not (tmp_path / "absent" / name).exists()


def _outer_branch(root: Path, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the outer repository on *branch*; Git's own default branch is made to differ."""
    _git(root, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    global_config = root.parent / "global.gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = master\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(root.parent / "missing-system-config"))


@pytest.mark.parametrize(
    ("branch", "prior", "expected"),
    [
        ("main", None, "main"),
        ("release/next", None, "release/next"),
        ("main", "legacy", "legacy"),
        ("main", "partial", "main"),
    ],
    ids=["main", "release-next", "existing", "partial"],
)
def test_stealth_booley_dir_repository_follows_the_outer_branch(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    prior: str | None,
    expected: str,
) -> None:
    _outer_branch(project, branch, monkeypatch)
    inner = project / ".booley"
    if prior == "legacy":
        _git(inner, "init", "-b", "legacy")  # an existing repository is left alone
    elif prior == "partial":
        (inner / ".git").mkdir()  # a half-created repository is repaired

    _initialize(project)

    # Read HEAD from the repository's own directory: a missing `.git` would fall through to the
    # outer repository's branch.
    assert (inner / ".git" / "HEAD").read_text(encoding="utf-8") == f"ref: refs/heads/{expected}\n"


def test_check_only_names_the_inner_branch_without_creating_it(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _outer_branch(project, "release/next", monkeypatch)

    _initialize(project, check_only=True)

    assert not (project / ".booley" / ".git").exists()
    assert "git init -b release/next" in capsys.readouterr().out


def test_inner_git_init_failure_leaves_no_repository(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_run = subprocess.run

    def run(command: list[str], *args: object, **kwargs: object) -> object:
        if "init" in command and "-b" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return real_run(command, *args, **kwargs)  # pyright: ignore[reportCallIssue]

    monkeypatch.setattr(subprocess, "run", run)

    _initialize(project)

    assert "could not `git init`" in capsys.readouterr().out
    assert not (project / ".booley" / ".git").exists()


def test_detached_outer_checkout_defers_the_inner_repository(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    _git(project, *identity, "commit", "--allow-empty", "-m", "initial")
    _git(project, "checkout", "--detach")

    for check_only in (True, False):
        results = _initialize(project, check_only=check_only)
        deferred = [r for r in results if r.name == "booley_dir" and r.status == "warn"]
        assert any("inner git branch unavailable" in r.detail for r in deferred)
        assert "attach the outer checkout to a branch" in capsys.readouterr().out
        assert not (project / ".booley" / ".git").exists()

    _git(project, "switch", "-c", "feature/retry")
    _initialize(project)

    assert _git(project / ".booley", "symbolic-ref", "HEAD") == "refs/heads/feature/retry"
