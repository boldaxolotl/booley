"""Tests for `booley init` git-side steps: Project Git hooks and worktree policies."""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from booley.harness import init_cmd
from booley.harness.setup import git_hooks, line_endings
from booley.harness.setup import project_git_hook_reconcile as reconcile
from booley.harness.setup.common import InitContext, StepResult
from booley.harness.setup.git_hooks import (
    WORKTREE_PRUNE_KEY,
    WORKTREE_PRUNE_VALUE,
    WORKTREE_RELATIVE_KEY,
    read_worktree_prune_expire,
)
from booley.harness.setup.guidance_links import plan_guidance_links
from booley.harness.setup.line_endings import (
    AutocrlfSetting,
    LineEndingMode,
    LineEndingRefusalReason,
    LineEndingReport,
    LineEndingRepository,
    LineEndingStatus,
    RepositoryLineEndingReport,
    discover_line_ending_repositories,
    read_autocrlf_setting,
    reconcile_project_line_endings,
    sample_worktree_cleanliness,
)
from booley.harness.setup.line_endings import (
    LineEndingActionKind as ActionKind,
)
from booley.harness.setup.line_endings import (
    LineEndingActionResult as ActionResult,
)
from booley.harness.setup.line_endings import (
    LineEndingActionState as ActionState,
)
from booley.harness.setup.line_endings import (
    LineEndingObservation as Observation,
)
from booley.harness.setup.line_endings import (
    LineEndingObservationCode as ObservationCode,
)
from booley.harness.setup.plan import InitPlan
from booley.harness.setup.project_git_hook_bundle import (
    BUNDLE_NAME,
    build_project_git_hook_bundle,
)
from booley.harness.setup.project_git_hook_reconcile import step_project_git_hooks
from booley.runtime.booley_dir import init_booley_dir_scope, reset_cache, resolve_booley_dir
from tests.conftest import symlink_or_skip
from tests.harness.booley_dir_support import select_booley_dir
from tests.subprocess_budget import run_bounded_child

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


def _git_init(root: Path) -> None:
    run_bounded_child(["git", "init", "-q", str(root)], check=True)


def _ctx(root: Path, *, check_only: bool = False, fix_line_endings: bool = False) -> InitContext:
    return InitContext(
        project_root=root,
        check_only=check_only,
        fix_line_endings=fix_line_endings,
        interactive=False,
    )


def _run_git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command for integration tests."""
    return run_bounded_child(["git", "-C", str(root), *args], check=check)


def _policy_project(root: Path, *, data_repository: bool = True) -> Path:
    """Initialize *root* as a Git repository with a `.booley` directory; return that directory."""
    _git_init(root)
    data = root / ".booley"
    data.mkdir()
    if data_repository:
        _git_init(data)
    return data


def _initialize(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    check_only: bool = False,
    host: tuple[int, int, int] | None = (2, 53, 0),
    sandbox: tuple[int, int, int] | None = (2, 53, 0),
    guidance_plan: InitPlan | None = None,
    line_ending_baseline: Mapping[Path, bool | None] | None = None,
) -> dict[str, StepResult]:
    """Run one Project Initialization and return its step results by name."""
    # Private seam: the host Git version is the real `git --version`; no Project
    # Initialization input sets it.
    monkeypatch.setattr(git_hooks, "_host_git_version", lambda: host)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    ctx = InitContext(
        project_root=root, check_only=check_only, interactive=False, show_step_banners=False
    )
    request = init_cmd.ProjectInitialization(
        None,
        root / ".booley" / "booley.toml",
        lambda: init_cmd.ProjectFacts(sandbox),
        reconcile_interactive_files=False,
        guidance_plan=guidance_plan,
        line_ending_baseline=line_ending_baseline,
    )
    try:
        with init_booley_dir_scope(root):
            init_cmd.initialize_project(ctx, request)
    finally:
        reset_cache()
    return {result.name: result for result in ctx.results}


@pytest.mark.parametrize(
    ("before", "status"),
    [(None, "ok"), ("3.months.ago", "ok"), (WORKTREE_PRUNE_VALUE, "current")],
)
def test_prune_guard_disables_pruning_in_both_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str | None, status: str
):
    data = _policy_project(tmp_path)
    for repository in (tmp_path, data):
        if before is not None:
            _run_git(repository, "config", WORKTREE_PRUNE_KEY, before)

    results = _initialize(tmp_path, monkeypatch)

    assert results["worktree_prune_guard"].status == status
    assert read_worktree_prune_expire(tmp_path) == WORKTREE_PRUNE_VALUE
    assert read_worktree_prune_expire(data) == WORKTREE_PRUNE_VALUE


def test_check_only_reports_worktree_policies_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = _policy_project(tmp_path)

    results = _initialize(tmp_path, monkeypatch, check_only=True)

    assert results["worktree_prune_guard"].detail == "would set"
    assert results["worktree_link_policy"].detail == "would set true"
    for repository in (tmp_path, data):
        assert read_worktree_prune_expire(repository) is None
        assert git_hooks.read_local_config(repository, WORKTREE_RELATIVE_KEY) is None


def test_prune_guard_skips_a_project_root_outside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / ".booley").mkdir()

    results = _initialize(tmp_path, monkeypatch)

    prune = results["worktree_prune_guard"]
    assert (prune.status, prune.detail) == ("skip", "not a git repo")


@pytest.mark.parametrize(
    ("host", "sandbox", "relative", "status", "detail"),
    [
        ((2, 48, 0), (2, 53, 0), "true", "ok", "relative links enabled"),
        ((2, 53, 0), (2, 47, 9), "false", "warn", "absolute-link fallback"),
        ((2, 53, 0), None, "false", "warn", "absolute-link fallback"),
    ],
)
def test_link_policy_follows_the_older_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: tuple[int, int, int],
    sandbox: tuple[int, int, int] | None,
    relative: str,
    status: str,
    detail: str,
):
    data = _policy_project(tmp_path)

    results = _initialize(tmp_path, monkeypatch, host=host, sandbox=sandbox)

    link = results["worktree_link_policy"]
    assert (link.status, link.detail) == (status, detail)
    for repository in (tmp_path, data):
        assert git_hooks.read_local_config(repository, WORKTREE_RELATIVE_KEY) == relative


def test_link_policy_keeps_relative_worktrees_after_a_git_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _policy_project(tmp_path, data_repository=False)
    _run_git(tmp_path, "config", "core.repositoryFormatVersion", "1")
    _run_git(tmp_path, "config", "extensions.relativeWorktrees", "true")
    _run_git(tmp_path, "config", WORKTREE_RELATIVE_KEY, "true")

    results = _initialize(tmp_path, monkeypatch, host=(2, 47, 9))

    link = results["worktree_link_policy"]
    assert (link.status, link.detail) == ("warn", "incompatible Git downgrade")
    assert git_hooks.read_local_config(tmp_path, WORKTREE_RELATIVE_KEY) == "true"


def test_config_write_failure_rolls_back_the_link_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data = _policy_project(tmp_path)
    # Git refuses to write a locked config, so the second repository's write fails.
    (data / ".git" / "config.lock").write_bytes(b"")

    results = _initialize(tmp_path, monkeypatch)

    assert results["worktree_prune_guard"].detail == "git config failed"
    assert results["worktree_link_policy"].detail == "git config failed"
    assert git_hooks.read_local_config(tmp_path, WORKTREE_RELATIVE_KEY) is None


@pytest.mark.parametrize("project_data", ["custom", "missing"])
def test_policy_repositories_follow_the_resolved_booley_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_data: str
):
    _git_init(tmp_path)
    custom = tmp_path / "state"
    custom.mkdir()
    _git_init(custom)

    def resolve(_root: Path) -> Path:
        if project_data == "missing":
            raise FileNotFoundError
        return custom

    monkeypatch.setattr(git_hooks, "resolve_checkout_booley_dir", resolve)

    repositories = git_hooks.worktree_policy_repositories(tmp_path)

    assert repositories == ((tmp_path, custom) if project_data == "custom" else (tmp_path,))


# Private seam: Git failing to start (OSError) cannot be staged per call; a Project
# Initialization runs Git in every step.
def test_git_probe_errors_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        git_hooks.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )

    assert git_hooks.read_local_config(tmp_path, WORKTREE_RELATIVE_KEY) is None
    assert git_hooks._host_git_version() is None
    assert git_hooks._set_local_config(tmp_path, "true") == "unavailable"


# Private seam: a rollback write failing after its forward write succeeded needs a
# per-call failure; real Git takes the same config lock for both.
def test_rollback_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    data = _policy_project(tmp_path)
    original = git_hooks._set_local_config
    calls: list[tuple[Path, str | None]] = []

    def fail_second_and_rollback(repository: Path, value: str | None) -> str | None:
        calls.append((repository.resolve(), value))
        if repository.resolve() == data.resolve():
            return "write failed"
        if repository.resolve() == tmp_path.resolve() and value is None:
            return "rollback failed"
        return original(repository, value)

    monkeypatch.setattr(git_hooks, "_set_local_config", fail_second_and_rollback)

    results = _initialize(tmp_path, monkeypatch)

    assert results["worktree_link_policy"].detail == "git config failed"
    root, booley_dir = tmp_path.resolve(), data.resolve()
    assert calls == [(root, "true"), (booley_dir, "true"), (root, None)]
    assert f"rollback {tmp_path}: rollback failed" in capsys.readouterr().out


class TestProjectCommitMsgHookVendoring:
    """Project Git policy is one managed bundle plus two outer adapters."""

    def test_vendors_runtime_helpers_alongside_scripts(self, tmp_path: Path, monkeypatch, request):
        select_booley_dir(request, monkeypatch, tmp_path)
        _git_init(tmp_path)
        ctx = _ctx(tmp_path)

        step_project_git_hooks(ctx)

        booley_dir = resolve_booley_dir(tmp_path)
        assert (booley_dir / ".managed" / BUNDLE_NAME).is_file()
        # The step creates only `.managed/`; no retired helper is vendored under `hooks/`.
        assert not (booley_dir / "hooks").exists()
        assert ctx.results[-1].name == "project_git_hooks"
        assert ctx.results[-1].status == "ok"

    def test_installed_hook_is_lf_not_crlf(self, tmp_path: Path, monkeypatch, request):
        """QA_REPORT D0a: the hook must be written with LF newlines.

        On a Windows host, Path.write_text with the default newline translates
        every \\n to \\r\\n, so the shebang lands as ``#!/bin/sh\\r``. The Linux
        Sandbox then tries to exec ``/bin/sh\\r`` (ENOENT) and EVERY
        in-container commit fails — hence all of Ticket Mode.
        """

        select_booley_dir(request, monkeypatch, tmp_path)
        _git_init(tmp_path)
        ctx = _ctx(tmp_path)

        step_project_git_hooks(ctx)

        hook = tmp_path / ".git" / "hooks" / "commit-msg"
        assert hook.is_file(), "commit-msg hook not installed"
        assert b"\r" not in hook.read_bytes(), "hook written with CRLF (D0a)"

    def test_installed_hook_rejects_compound_attribution_without_rewriting(
        self, tmp_path: Path, monkeypatch, request: pytest.FixtureRequest
    ):
        """The standalone bundle hook rejects before either attribution form is altered."""
        repo, baseline = self._stealth_hook_repo(
            tmp_path,
            monkeypatch,
            request,
            '[stealth]\nenabled = true\nbanned_words = ["generated", "assistant-identity"]\n',
        )

        (repo / "change.txt").write_text("change\n", encoding="utf-8")
        _run_git(repo, "add", "change.txt")
        original = (
            "fix(core): repair edge case\n\n"
            "Useful rationale.\n\n"
            "Co-Authored-By: Person <person@example.test>\n"
            "Generated with assistant-identity\n"
        )
        message = tmp_path / "message.txt"
        message.write_text(original, encoding="utf-8")

        result = _run_git(repo, "commit", "-F", str(message), check=False)

        assert result.returncode != 0
        assert _run_git(repo, "rev-parse", "HEAD").stdout.strip() == baseline
        assert (repo / ".git" / "COMMIT_EDITMSG").read_text(encoding="utf-8") == original
        assert "assistant-identity" not in result.stderr
        assert "person@example.test" not in result.stderr
        assert "remove" in result.stderr.lower()
        assert "retry" in result.stderr.lower()

    def _stealth_hook_repo(
        self, tmp_path: Path, monkeypatch, request: pytest.FixtureRequest, config_text: str
    ) -> tuple[Path, str]:
        """Return a repository with the installed commit-msg hook and its baseline commit."""
        repo = tmp_path / "project"
        _run_git(tmp_path, "init", "-q", "-b", "main", str(repo))
        _run_git(repo, "config", "user.name", "T")
        _run_git(repo, "config", "user.email", "t@example.test")
        (repo / ".gitignore").write_text(".booley/\n", encoding="utf-8")
        config = repo / ".booley" / "booley.toml"
        config.parent.mkdir()
        config.write_text(config_text, encoding="utf-8")
        (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        _run_git(repo, "add", ".gitignore", "baseline.txt")
        _run_git(repo, "commit", "-qm", "baseline")
        baseline = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

        booley_dir = tmp_path / "project-state"
        booley_dir.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
        reset_cache()
        request.addfinalizer(reset_cache)
        step_project_git_hooks(_ctx(repo))
        return repo, baseline

    @pytest.mark.parametrize("mode", ["staged", "all"])
    def test_rejected_banned_message_retries_with_same_text(
        self, tmp_path: Path, monkeypatch, request: pytest.FixtureRequest, mode: str
    ):
        """A banned-term rejection keeps the text and the printed command reuses it."""
        repo, baseline = self._stealth_hook_repo(
            tmp_path,
            monkeypatch,
            request,
            '[stealth]\nenabled = true\nbanned_words = ["quokka"]\n',
        )
        if mode == "staged":
            (repo / "change.txt").write_text("change\n", encoding="utf-8")
            _run_git(repo, "add", "change.txt")
            options = ["commit"]
        else:
            (repo / "baseline.txt").write_text("changed\n", encoding="utf-8")
            options = ["commit", "-a"]
        original = "fix(core): repair quokka path\n\nBody line.\n"
        message = tmp_path / "message.txt"
        message.write_text(original, encoding="utf-8")

        rejected = _run_git(repo, *options, "-F", str(message), check=False)

        kept = (repo / ".git" / "COMMIT_EDITMSG").resolve()
        assert rejected.returncode != 0
        assert _run_git(repo, "rev-parse", "HEAD").stdout.strip() == baseline
        assert kept.read_text(encoding="utf-8") == original
        assert f'-v -e -F "{kept}"' in rejected.stderr
        assert "-a" in rejected.stderr and "--amend" in rejected.stderr
        assert "'quokka' in the subject" in rejected.stderr

        # Remove the term from the vocabulary and rerun the printed command.
        (repo / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nbanned_words = []\n", encoding="utf-8"
        )
        env = {**os.environ, "GIT_EDITOR": "true"}
        retried = run_bounded_child(
            ["git", "-C", str(repo), *options, "-v", "-e", "-F", str(kept)], env=env
        )
        assert retried.returncode == 0, retried.stderr
        assert _run_git(repo, "log", "-1", "--format=%B").stdout.strip() == original.strip()
        files = _run_git(repo, "show", "--name-only", "--format=").stdout.split()
        assert files == (["change.txt"] if mode == "staged" else ["baseline.txt"])

    def test_generated_merge_subject_rejected_with_merge_hint(
        self, tmp_path: Path, monkeypatch, request: pytest.FixtureRequest
    ):
        """Git's generated merge subject can carry a banned branch name; say how to finish."""
        repo, baseline = self._stealth_hook_repo(
            tmp_path, monkeypatch, request, "[stealth]\nenabled = true\n"
        )
        _run_git(repo, "checkout", "-q", "-b", "booley/feature")
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        _run_git(repo, "add", "feature.txt")
        _run_git(repo, "commit", "-qm", "fix(core): feature work")
        _run_git(repo, "checkout", "-q", "main")

        rejected = _run_git(repo, "merge", "--no-ff", "--no-edit", "booley/feature", check=False)

        assert rejected.returncode != 0
        assert _run_git(repo, "rev-parse", "HEAD").stdout.strip() == baseline
        assert "'booley' in the subject" in rejected.stderr
        assert 'git commit -m "<message>"' in rejected.stderr
        assert "git merge" in rejected.stderr and '-m "<message>"' in rejected.stderr

        # The merge is still in progress, so a message from -m is also a merge message.
        again = _run_git(repo, "commit", "-m", "fix(core): booley merge", check=False)
        assert again.returncode != 0
        assert 'git commit -v -e -F "' in again.stderr

        _run_git(repo, "commit", "-m", "Merge feature work")
        parents = _run_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        assert len(parents) == 3


@pytest.mark.parametrize("runtime_dir", [None, "runtime-project-state"])
def test_tracked_project_state_is_refused_at_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, runtime_dir
):
    main = tmp_path / "main"
    origin = tmp_path / "origin"
    _run_git(tmp_path, "init", "-q", str(main))
    _run_git(tmp_path, "init", "-q", "--bare", str(origin))
    _run_git(main, "remote", "add", "origin", str(origin))
    (main / ".booley").mkdir()
    (main / ".booley" / "booley.toml").write_text("[stealth]\nenabled = true\n")
    if runtime_dir is None:
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
    else:
        (tmp_path / runtime_dir).mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / runtime_dir))
    reset_cache()
    request.addfinalizer(reset_cache)
    step_project_git_hooks(_ctx(main))

    leaked = main / ".booley" / "docs" / "example.md"
    leaked.parent.mkdir(parents=True)
    leaked.write_text("private project state\n", encoding="utf-8")
    _run_git(main, "add", "-f", ".booley/docs/example.md")
    _run_git(
        main,
        *("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "docs", "--no-verify"),
    )

    result = _run_git(main, "push", "origin", "HEAD:main", check=False)

    assert result.returncode != 0, "tracked Project state reached the remote"
    assert "tracked path exposes project state" in result.stderr
    assert ".booley/docs/example.md" in result.stderr
    assert not _run_git(origin, "for-each-ref", "--format=%(refname)").stdout.strip()


# File names older releases vendored into the Booley Directory's `hooks/`; the migration
# step removes or backs them up.
LEGACY_HOOKS = (
    "boundary.py",
    "checkout_role.py",
    "run_command.py",
    "commit_msg_utils.py",
    "validate_commit_msg.py",
    "commit_msg_hook.py",
    "pre_push_hook.py",
)


def _bundle_sources() -> dict[str, bytes]:
    """Return the canonical bytes of every hook source the bundle ships, by file name."""
    with zipfile.ZipFile(io.BytesIO(build_project_git_hook_bundle().content)) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name.endswith(".py") and name != "__main__.py"
        }


def _tree_snapshot(root: Path) -> dict[Path, tuple[bytes | None, int]]:
    """Capture bytes and modes of every path below *root* without following links."""
    return {
        path: (path.read_bytes() if path.is_file() else None, path.lstat().st_mode)
        for path in sorted(root.rglob("*"))
    }


class TestProjectGitHookMigration:
    @pytest.fixture(autouse=True)
    def booley_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        (tmp_path / ".booley").mkdir()
        reset_cache()
        yield
        reset_cache()

    @staticmethod
    def _seed(root: Path, files: dict[str, bytes]) -> Path:
        """Install hooks once, then write *files* into the Booley Directory's hooks/."""
        _git_init(root)
        step_project_git_hooks(_ctx(root))
        hooks = root / ".booley" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name, data in files.items():
            (hooks / name).write_bytes(data)
        return hooks

    def test_check_only_reports_current_installation_without_mutation(self, tmp_path: Path):
        self._seed(tmp_path, {})
        before = _tree_snapshot(tmp_path)

        ctx = _ctx(tmp_path, check_only=True)
        step_project_git_hooks(ctx)

        assert (ctx.results[-1].status, ctx.results[-1].detail) == ("current", "current")
        assert _tree_snapshot(tmp_path) == before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
    def test_check_only_reports_non_executable_delegator_without_chmod(self, tmp_path: Path):
        self._seed(tmp_path, {})
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.chmod(0o644)

        ctx = _ctx(tmp_path, check_only=True)
        step_project_git_hooks(ctx)

        assert ctx.results[-1].detail == "update pre-push"
        assert hook.stat().st_mode & 0o111 == 0

    def test_symlinked_adapter_is_refused(self, tmp_path: Path):
        self._seed(tmp_path, {})  # installs both adapters
        git_hooks_dir = tmp_path / ".git" / "hooks"
        outside = tmp_path / "outside-commit-msg"
        outside.write_text("# someone else's hook\n", encoding="utf-8")
        (git_hooks_dir / "commit-msg").unlink()
        symlink_or_skip(git_hooks_dir / "commit-msg", outside)
        planted_target = (git_hooks_dir / "commit-msg").readlink()
        (git_hooks_dir / "pre-push").write_text("# stale\n", encoding="utf-8")

        check = _ctx(tmp_path, check_only=True)
        step_project_git_hooks(check)
        assert "refuse symlinked commit-msg adapter" in check.results[-1].detail
        assert "back up existing pre-push adapter" in check.results[-1].detail

        ctx = _ctx(tmp_path)
        step_project_git_hooks(ctx)

        assert ctx.results[-1].status == "err"
        assert "commit-msg adapter: refused" in ctx.results[-1].detail
        assert (git_hooks_dir / "commit-msg").readlink() == planted_target
        assert outside.read_text(encoding="utf-8") == "# someone else's hook\n"
        assert "pre_push_hook.py" in (git_hooks_dir / "pre-push").read_text(encoding="utf-8")

    def test_non_git_project_skips_project_hook_reconciliation(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        step_project_git_hooks(ctx)

        assert ctx.results[-1].status == "skip"
        assert ctx.results[-1].detail == "not a git repo"

    def test_git_hook_path_timeout_skips_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), 10)

        monkeypatch.setattr(reconcile.subprocess, "run", timeout)
        ctx = _ctx(tmp_path)
        step_project_git_hooks(ctx)

        assert ctx.results[-1].status == "skip"
        assert "timed out" in ctx.results[-1].detail

    @staticmethod
    def _installed_bundle_line(root: Path) -> str:
        """Install the adapters in *root* and return the `BUNDLE=` line of the commit-msg one."""
        reset_cache()
        _git_init(root)
        step_project_git_hooks(_ctx(root))
        hook = (root / ".git" / "hooks" / "commit-msg").read_text(encoding="utf-8")
        return next(line for line in hook.splitlines(keepends=True) if line.startswith("BUNDLE="))

    def test_adapter_reaches_an_external_bundle_by_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(external))
        reset_cache()

        line = self._installed_bundle_line(tmp_path / "project")

        bundle = external / ".managed" / BUNDLE_NAME
        assert line == f"BUNDLE={shlex.quote(str(bundle.resolve()))}\n"

    def test_adapter_follows_only_the_booley_dir_entry_link(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        symlink_or_skip(root / ".booley", self._real_directory(tmp_path))

        linked = self._installed_bundle_line(root)

        assert linked == f'BUNDLE="$ROOT/.booley/.managed/{BUNDLE_NAME}"\n'

        # A link below the entry still points outside the checkout: absolute fallback.
        plain = tmp_path / "plain"
        (plain / ".booley").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        symlink_or_skip(plain / ".booley" / ".managed", elsewhere)
        below = self._installed_bundle_line(plain)
        assert below == f"BUNDLE={shlex.quote(str(elsewhere / BUNDLE_NAME))}\n"

    def test_adapter_follows_a_symlinked_relocated_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
        symlink_or_skip(root / "state", self._real_directory(tmp_path))

        linked = self._installed_bundle_line(root)

        assert linked == f'BUNDLE="$ROOT/state/.managed/{BUNDLE_NAME}"\n'

    @staticmethod
    def _real_directory(tmp_path: Path) -> Path:
        real = tmp_path / "real-booley-dir"
        real.mkdir()
        return real

    def test_check_only_lists_every_pending_action_without_writing(self, tmp_path: Path) -> None:
        hooks = self._seed(
            tmp_path,
            {"boundary.py": _bundle_sources()["boundary.py"], "run_command.py": b"edit\n"},
        )
        (hooks / "__pycache__").mkdir()
        (hooks / "__pycache__" / "boundary.cpython-314.pyc").write_bytes(b"managed")
        (tmp_path / ".booley" / ".managed" / "project-git-hooks.pyz").unlink()
        commit_msg = tmp_path / ".git" / "hooks" / "commit-msg"
        commit_msg.write_bytes(commit_msg.read_bytes() + b"# older revision\n")
        (commit_msg.parent / "pre-push").write_bytes(b"#!/bin/sh\necho foreign\n")
        before = _tree_snapshot(tmp_path)

        ctx = _ctx(tmp_path, check_only=True)
        step_project_git_hooks(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail.split("; ") == [
            "publish project-git-hooks.pyz",
            "update commit-msg",
            "back up existing pre-push adapter",
            "remove legacy boundary.py",
            "rename divergent run_command.py to run_command.py.pre-booley",
            "remove managed bytecode boundary.cpython-314.pyc",
            "remove empty hooks/__pycache__",
        ]
        assert _tree_snapshot(tmp_path) == before

    def test_crlf_only_legacy_sources_are_not_backed_up(self, tmp_path: Path) -> None:
        hooks = self._seed(
            tmp_path,
            {name: _bundle_sources()[name].replace(b"\n", b"\r\n") for name in LEGACY_HOOKS},
        )
        (hooks / "__pycache__").mkdir()
        (hooks / "__pycache__" / "boundary.cpython-314.pyc").write_bytes(b"managed")

        step_project_git_hooks(_ctx(tmp_path))

        assert not list(hooks.glob("*.pre-booley*"))
        assert not any((hooks / name).exists() for name in LEGACY_HOOKS)
        assert not (hooks / "__pycache__").exists()

    def test_legacy_sources_and_managed_bytecode_are_removed(self, tmp_path: Path) -> None:
        sources = _bundle_sources()
        hooks = self._seed(tmp_path, {**sources, "post-setup.sh": b"#!/bin/sh\n"})
        cache = hooks / "__pycache__"
        cache.mkdir()
        (cache / "boundary.cpython-314.pyc").write_bytes(b"managed")
        (cache / "booley_commit_policy.cpython-314.pyc").write_bytes(b"foreign")

        step_project_git_hooks(_ctx(tmp_path))

        assert not any((hooks / name).exists() for name in LEGACY_HOOKS)
        assert all((hooks / name).is_file() for name in set(sources) - set(LEGACY_HOOKS))
        assert (hooks / "post-setup.sh").is_file()
        assert not (cache / "boundary.cpython-314.pyc").exists()
        assert (cache / "booley_commit_policy.cpython-314.pyc").is_file()

    def test_divergent_legacy_source_is_renamed_to_pre_booley(
        self, tmp_path: Path, capsys
    ) -> None:
        hooks = self._seed(tmp_path, {"boundary.py": b"locally modified\n"})

        step_project_git_hooks(_ctx(tmp_path))

        assert not (hooks / "boundary.py").exists()
        assert (hooks / "boundary.py.pre-booley").read_bytes() == b"locally modified\n"
        assert "boundary.py to boundary.py.pre-booley" in capsys.readouterr().out

    def test_divergent_legacy_source_is_kept_when_backup_name_is_taken(
        self, tmp_path: Path, capsys
    ) -> None:
        files = {"boundary.py": b"locally modified\n", "boundary.py.pre-booley": b"older\n"}
        hooks = self._seed(tmp_path, files)

        step_project_git_hooks(_ctx(tmp_path))

        assert {name: (hooks / name).read_bytes() for name in files} == files
        assert "keep divergent boundary.py" in capsys.readouterr().out

    @pytest.mark.skipif(os.name == "nt", reason="symlink privileges are unavailable on CI")
    def test_non_regular_legacy_paths_are_preserved(self, tmp_path: Path, capsys) -> None:
        hooks = self._seed(tmp_path, {})
        (tmp_path / "boundary-copy.py").write_bytes(_bundle_sources()["boundary.py"])
        (hooks / "boundary.py").symlink_to(tmp_path / "boundary-copy.py")
        (hooks / "checkout_role.py").mkdir()
        (hooks / "run_command.py").symlink_to(tmp_path / "missing.py")

        step_project_git_hooks(_ctx(tmp_path))

        assert (hooks / "boundary.py").is_symlink()
        assert (hooks / "checkout_role.py").is_dir()
        assert (hooks / "run_command.py").is_symlink()
        out = capsys.readouterr().out
        for name in ("boundary.py", "checkout_role.py", "run_command.py"):
            assert f"unrecognized legacy path {name}" in out

    def test_adapter_write_failure_keeps_hooks_working_and_rerun_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_git(tmp_path, "init", "-q", "--bare", str(tmp_path / ".booley" / "origin.git"))
        hooks = self._seed(tmp_path, {name: _bundle_sources()[name] for name in LEGACY_HOOKS})
        (tmp_path / ".booley" / ".managed" / "project-git-hooks.pyz").unlink()
        pre_push = tmp_path / ".git" / "hooks" / "pre-push"
        pre_push.write_bytes(pre_push.read_bytes() + b"# older revision\n")
        real_write = reconcile.guarded_write

        def fail_pre_push(target: Path, *args: Any, **kwargs: Any) -> Any:
            if target.name == "pre-push" and not kwargs.get("dry_run"):
                raise OSError("simulated adapter write failure")
            return real_write(target, *args, **kwargs)

        monkeypatch.setattr(reconcile, "guarded_write", fail_pre_push)
        failed = _ctx(tmp_path)
        step_project_git_hooks(failed)

        assert failed.results[-1].status == "err"
        assert all((hooks / name).is_file() for name in LEGACY_HOOKS)
        (tmp_path / ".gitignore").write_text(".booley/\n", encoding="utf-8")
        _run_git(tmp_path, "add", ".gitignore")
        commit = _run_git(
            tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "chore: ignore"
        )
        push = _run_git(
            tmp_path, "push", str(tmp_path / ".booley" / "origin.git"), "HEAD:main", check=False
        )
        assert "bundle not found" not in commit.stderr + push.stderr
        assert push.returncode == 0, push.stderr

        monkeypatch.setattr(reconcile, "guarded_write", real_write)
        step_project_git_hooks(_ctx(tmp_path))
        check = _ctx(tmp_path, check_only=True)
        step_project_git_hooks(check)
        assert check.results[-1].detail == "current"
        assert not any((hooks / name).exists() for name in LEGACY_HOOKS)


@pytest.fixture
def bundle_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A repository with a bare origin, the managed bundle, and both adapters."""
    main = tmp_path / "main"
    _run_git(tmp_path, "init", "-q", "-b", "main", str(main))
    _run_git(tmp_path, "init", "-q", "--bare", str(tmp_path / "origin"))
    _run_git(main, "remote", "add", "origin", str(tmp_path / "origin"))
    for key, val in (("user.email", "t@example.com"), ("user.name", "T")):
        _run_git(main, "config", key, val)
    (main / ".booley").mkdir()
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    step_project_git_hooks(_ctx(main))
    (main / ".gitignore").write_text(".booley/\n", encoding="utf-8")
    _run_git(main, "add", ".gitignore")
    _run_git(main, "commit", "-qm", "init")
    yield main
    reset_cache()


def _commit(repo: Path, msg: str) -> subprocess.CompletedProcess[str]:
    (repo / f"{msg}.txt").write_text("x", encoding="utf-8")
    _run_git(repo, "add", "-A")
    return _run_git(repo, "commit", "-m", msg, check=False)


class TestHookInSecondaryWorktree:
    """A user-made secondary worktree resolves the main checkout's bundle."""

    def test_commit_succeeds_in_a_secondary_worktree(self, tmp_path: Path, bundle_repo: Path):
        main = bundle_repo
        wt = tmp_path / "wt"
        _run_git(main, "worktree", "add", "-q", str(wt), "-b", "side")

        result = _commit(wt, "from-worktree")

        assert result.returncode == 0, result.stderr
        # It did not merely skip: the bundle was found via the shared git dir.
        assert "bundle not found" not in result.stderr
        assert _run_git(wt, "log", "-1", "--format=%s").stdout.strip() == "from-worktree"

    def test_missing_script_skips_instead_of_blocking_the_commit(
        self, tmp_path: Path, bundle_repo: Path
    ):
        main = bundle_repo
        (main / ".booley" / ".managed" / "project-git-hooks.pyz").unlink()

        result = _commit(main, "no-script")

        assert result.returncode == 0, result.stderr
        assert "project-git-hooks.pyz" in result.stderr

    def test_main_worktree_still_runs_the_hook(self, tmp_path: Path, bundle_repo: Path):
        main = bundle_repo

        result = _commit(main, "from-main")

        assert result.returncode == 0, result.stderr
        assert "bundle not found" not in result.stderr
        assert _run_git(main, "log", "-1", "--format=%s").stdout.strip() == "from-main"


class TestPrePushFailsClosed:
    """The missing-bundle fallback must differ per hook.

    commit-msg is a convenience — a missing script skips so local work is never
    wedged (F-42). pre-push is the leak guard (F-17): banned-term scan plus the
    `[stealth] allowed_authors` allowlist. `.booley/` is git-ignored, so
    `git clean -xdf` deletes the managed bundle while `.git/hooks/pre-push`
    survives; sharing commit-msg's `exit 0` there let the next push sail past
    both checks with one stderr line.
    """

    def test_push_succeeds_while_the_guard_is_installed(self, tmp_path: Path, bundle_repo: Path):
        main = bundle_repo

        result = _run_git(main, "push", "-u", "origin", "main", check=False)

        assert result.returncode == 0, result.stderr

    def test_missing_pre_push_script_blocks_the_push(self, tmp_path: Path, bundle_repo: Path):
        main = bundle_repo
        (main / ".booley" / ".managed" / "project-git-hooks.pyz").unlink()

        result = _run_git(main, "push", "-u", "origin", "main", check=False)

        assert result.returncode != 0, "fail-open leak guard: push went through"
        assert "REFUSED" in result.stderr
        assert "booley init" in result.stderr
        # And nothing reached the remote.
        assert not _run_git(tmp_path / "origin", "for-each-ref").stdout.strip()

    def test_adapters_probe_an_isolated_python(self, bundle_repo: Path):
        """F-7: a bare `exec python3` breaks every commit on stock Windows (the Store
        alias exits non-zero), so each candidate interpreter is RUN in the isolated mode
        used for the real invocation."""
        for name in ("commit-msg", "pre-push"):
            adapter = (bundle_repo / ".git" / "hooks" / name).read_text(encoding="utf-8")

            assert adapter.startswith("#!/bin/sh\n")
            assert (
                "    if \"$cand\" -I -S -c '' >/dev/null 2>&1; then PY=$cand; break; fi\n"
                in adapter
            )
            assert "py -3 -I -S -c ''" in adapter
            assert f'exec $PY -I -S "$BUNDLE" {name} "$@"' in adapter


LF = b"module a;\nendmodule\n"
CRLF = b"module a;\r\nendmodule\r\n"
GUIDANCE = b"guidance\n"
_NEEDS_HARDLINKS = pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")


def _repo(
    root: Path,
    autocrlf: str | None = "true",
    files: dict[str, bytes] | None = None,
    *,
    crlf: tuple[str, ...] = (),
) -> Path:
    """Create a repository with *files* committed; Git itself checks the *crlf* names out.

    A hand-written CRLF file leaves a stale stat cache, so `git status` would report it
    modified, which a real CRLF checkout does not; delete and `checkout --` instead.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git_init(root)
    if autocrlf is not None:
        _run_git(root, "config", "core.autocrlf", autocrlf)
    for name, data in (files or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
        _run_git(root, "add", "-f", name)
    if files:
        _run_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    for name in crlf:
        (root / name).unlink()
        _run_git(root, "checkout", "--", name)
    return root


def _tracked_clean(root: Path) -> bool:
    status = _run_git(root, "status", "--porcelain", "--untracked-files=no").stdout
    return (
        status == "" and _run_git(root, "diff", "--cached", "--quiet", check=False).returncode == 0
    )


def _init(root: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, StepResult]:
    """Run Project Initialization with a Booley Directory present."""
    (root / ".booley").mkdir(exist_ok=True)
    return _initialize(root, monkeypatch, **kwargs)


def _guidance_project(root: Path) -> Path:
    """Project whose CRLF canonical guidance carries Booley's root hardlinks."""
    _repo(root, None)
    data = _repo(root / ".booley", "true", {"AGENTS.md": GUIDANCE}, crlf=("AGENTS.md",))
    for name in ("AGENTS.md", "CLAUDE.md"):
        os.link(data / "AGENTS.md", root / name)
    return data


def _initialize_with_report(
    root: Path, monkeypatch: pytest.MonkeyPatch, report: LineEndingReport
) -> dict[str, StepResult]:
    """Run Project Initialization with the line-ending reconciliation returning *report*."""
    monkeypatch.setattr(git_hooks, "reconcile_project_line_endings", lambda *_a, **_k: report)
    return _init(root, monkeypatch)


@pytest.mark.parametrize(
    ("autocrlf", "files", "crlf"),
    [
        pytest.param("false", {"a.v": LF}, (), id="lf-tree"),
        pytest.param("false", {"a.v": CRLF}, (), id="crlf-in-index"),
        pytest.param(
            "false",
            {".gitattributes": b"* text eol=lf\n*.bat -text\n", "run.bat": b"@echo off\r\n"},
            (),
            id="minus-text",
        ),
        pytest.param(
            "false",
            {".gitattributes": b"*.txt text eol=crlf\n", "a.txt": b"alpha\nbeta\n"},
            ("a.txt",),
            id="eol-crlf-attribute",
        ),
    ],
)
def test_safe_checkouts_pass_untouched(tmp_path, monkeypatch, autocrlf, files, crlf):
    _repo(tmp_path, autocrlf, files, crlf=crlf)
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    results = _init(tmp_path, monkeypatch)

    assert results["line_endings"].status == "ok"
    assert {path: path.read_bytes() for path in before} == before


def test_check_only_names_the_pending_eol_pin(tmp_path, monkeypatch, capsys):
    # A repository an earlier Booley pinned: only core.autocrlf is set locally.
    _repo(tmp_path, "false", {"a.v": LF})

    results = _init(tmp_path, monkeypatch, check_only=True)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "ok",
        "core.autocrlf/core.eol pin pending",
    )
    assert "working tree is container-safe" in capsys.readouterr().out


@pytest.mark.parametrize("dirt", ["crlf-edit", "staged-addition"])
def test_dirty_tree_blocks_the_repair(tmp_path, monkeypatch, capsys, dirt):
    # The F-15 shape: core.autocrlf=true stores LF and checks CRLF out, so the container's
    # Git, which converts nothing, would report every such file modified.
    _repo(tmp_path, "true", {"a.v": LF}, crlf=("a.v",) if dirt == "staged-addition" else ())
    if dirt == "crlf-edit":
        (tmp_path / "a.v").write_bytes(CRLF)
    else:
        (tmp_path / "b.v").write_bytes(LF)
        _run_git(tmp_path, "add", "b.v")

    results = _init(tmp_path, monkeypatch)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "err",
        "dirty tree",
    )
    out = capsys.readouterr().out
    assert "!  1 tracked file(s) are checked out with CRLF" in out
    # The pin is withheld: flipping autocrlf over a CRLF checkout would create phantom diffs.
    assert "would set core.autocrlf=false and core.eol=lf" in out
    assert (tmp_path / "a.v").read_bytes() == CRLF
    assert git_hooks.read_local_config(tmp_path, "core.autocrlf") == "true"


def test_failed_autocrlf_update_blocks_readiness(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, "true")
    (tmp_path / ".git" / "config.lock").write_bytes(b"")

    results = _init(tmp_path, monkeypatch)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "err",
        "autocrlf update failed",
    )
    out = capsys.readouterr().out
    assert "!  core.autocrlf=true" in out
    assert "·  detected core.autocrlf=true" not in out


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", None])
def test_repair_pins_autocrlf_false_in_both_repositories(tmp_path, value):
    data = _repo(tmp_path / ".booley", value)
    _repo(tmp_path, value)

    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    for repository in (tmp_path, data):
        assert git_hooks.read_local_config(repository, "core.autocrlf") == "false"


def test_not_a_git_repository_skips_the_step(tmp_path, monkeypatch):
    results = _initialize(tmp_path, monkeypatch)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "skip",
        "not a git repo",
    )


def test_separate_project_data_repository_is_normalized(tmp_path, monkeypatch):
    global_config = tmp_path / "global.gitconfig"
    global_config.write_bytes(b"[core]\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _repo(tmp_path, None)
    data = _repo(
        tmp_path / ".booley",
        None,
        {"hooks/post-setup.sh": b"#!/bin/sh\nset -u\n"},
        crlf=("hooks/post-setup.sh",),
    )
    hook = data / "hooks" / "post-setup.sh"
    assert b"\r\n" in hook.read_bytes()

    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert hook.read_bytes() == b"#!/bin/sh\nset -u\n"
    for repository in (tmp_path, data):
        assert git_hooks.read_local_config(repository, "core.autocrlf") == "false"
        assert _tracked_clean(repository)


@pytest.mark.parametrize(
    ("effective", "local", "code"),
    [
        (None, None, ObservationCode.AUTOCRLF_UNREADABLE),
        (False, None, ObservationCode.LOCAL_AUTOCRLF_UNREADABLE),
    ],
)
def test_unreadable_autocrlf_policy_fails_safe(tmp_path, monkeypatch, effective, local, code):
    values = {False: effective, True: local}

    def read_setting(_root: Path, *, local: bool = False) -> AutocrlfSetting | None:
        value = values[local]
        return None if value is None else AutocrlfSetting(value, is_set=True)

    _repo(tmp_path, None)
    monkeypatch.setattr(line_endings, "read_autocrlf_setting", read_setting)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.UNSAFE
    assert code in {item.code for item in report.repositories[0].observations}


def test_invalid_autocrlf_boolean_is_unreadable(tmp_path, monkeypatch):
    garbage = subprocess.CompletedProcess(
        ["git", "config"], 0, stdout="not-a-boolean\n", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: garbage)

    assert read_autocrlf_setting(tmp_path) is None


@pytest.mark.parametrize("separate", [False, True])
def test_line_ending_repositories_are_discovered_once(tmp_path, separate):
    _repo(tmp_path, None)
    data = tmp_path / ".booley"
    if separate:
        _repo(data, None)
    else:
        data.mkdir()

    discovery = discover_line_ending_repositories(tmp_path, data)

    assert discovery.failures == ()
    expected = [("project-checkout", tmp_path.resolve())]
    if separate:
        expected.append(("project-data", data.resolve()))
    assert [(repo.role, repo.root) for repo in discovery.repositories] == expected


def test_missing_project_data_prevents_aggregate_success(tmp_path, monkeypatch):
    _repo(tmp_path, "false", {"a.v": LF})

    results = _initialize(tmp_path, monkeypatch, check_only=True)

    assert results["line_endings"].status == "warn"
    assert "project-data: directory does not exist" in results["line_endings"].detail


def test_check_only_previews_without_touching_either_repository(tmp_path, monkeypatch, capsys):
    data = _repo(tmp_path / ".booley", "true")
    _repo(tmp_path, "true", {"a.v": LF}, crlf=("a.v",))

    results = _init(tmp_path, monkeypatch, check_only=True)

    assert results["line_endings"].status == "warn"
    out = capsys.readouterr().out
    assert "!  1 tracked file(s) are checked out with CRLF" in out
    assert "!  core.autocrlf=true" in out
    assert "would normalize 1 tracked file(s) to LF" in out
    assert "·  detected" not in out
    assert (tmp_path / "a.v").read_bytes() == CRLF
    for repository in (tmp_path, data):
        assert git_hooks.read_local_config(repository, "core.autocrlf") == "true"
        assert _tracked_clean(repository)


def test_user_dirty_project_data_is_refused_while_outer_is_repaired(tmp_path, monkeypatch):
    """#611: user work present before init keeps the repair refused."""
    _repo(tmp_path, "true")
    data = _repo(tmp_path / ".booley", "true", {"notes.md": b"notes\n"}, crlf=("notes.md",))
    with (data / "notes.md").open("ab") as notes:
        notes.write(b"user work\r\n")
    baseline = sample_worktree_cleanliness(tmp_path, data)
    assert baseline[data] is False

    results = _init(tmp_path, monkeypatch, line_ending_baseline=baseline)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "err",
        "project-data: dirty tree",
    )
    assert (data / "notes.md").read_bytes() == b"notes\r\nuser work\r\n"
    assert git_hooks.read_local_config(tmp_path, "core.autocrlf") == "false"
    assert _tracked_clean(tmp_path)


def test_clean_crlf_checkout_is_auto_fixed_in_one_run(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, "true", {"a.v": LF}, crlf=("a.v",))

    results = _init(tmp_path, monkeypatch)

    assert results["line_endings"].status == "ok"
    assert git_hooks.read_local_config(tmp_path, "core.autocrlf") == "false"
    assert (tmp_path / "a.v").read_bytes() == LF
    assert _tracked_clean(tmp_path)
    out = capsys.readouterr().out
    assert "·  detected 1 tracked file(s) are checked out with CRLF" in out
    assert "·  detected core.autocrlf=true" in out
    assert "!  " not in out
    rerun = _init(tmp_path, monkeypatch)
    assert (rerun["line_endings"].status, rerun["line_endings"].detail) == ("ok", "no CRLF")


def test_rerun_heals_stale_index_from_earlier_normalization(tmp_path, monkeypatch):
    _repo(tmp_path, "true", {"a.v": LF}, crlf=("a.v",))
    _run_git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "a.v").write_bytes(LF)
    assert not _tracked_clean(tmp_path)

    results = _init(tmp_path, monkeypatch)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "ok",
        "index refreshed",
    )
    assert _tracked_clean(tmp_path)


def test_partial_normalization_reports_normalized_and_left_files(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, "true", {"a.v": LF, "b.v": LF}, crlf=("a.v", "b.v"))
    baseline = sample_worktree_cleanliness(tmp_path)
    edited = b"module b;\r\n  localparam KEEP = 1;\r\nendmodule\r\n"
    (tmp_path / "b.v").write_bytes(edited)

    results = _init(tmp_path, monkeypatch, line_ending_baseline=baseline)

    assert results["line_endings"].status == "err"
    assert (tmp_path / "a.v").read_bytes() == LF
    assert (tmp_path / "b.v").read_bytes() == edited
    out = capsys.readouterr().out
    assert "normalized 1 file(s) to LF" in out
    assert "'b.v' (changed)" in out
    assert "would leave tracked files untouched" not in out


def test_phantom_status_failure_includes_git_context(tmp_path, monkeypatch):
    _repo(tmp_path, "false")
    real_run = subprocess.run

    def run(args: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess[Any]:
        if "status" in args and "--porcelain" in args:
            raise OSError("git unavailable")
        return real_run(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.UNSAFE
    (observation,) = [
        item
        for item in report.repositories[0].observations
        if item.code is ObservationCode.STATUS_UNREADABLE
    ]
    assert f"git -C {tmp_path} status --porcelain" in observation.detail
    assert "git unavailable" in observation.detail


def _skip_worktree_edit(root: Path) -> None:
    _run_git(root, "update-index", "--skip-worktree", "local.v")
    (root / "local.v").write_bytes(b"module local;\n  localparam int KEEP = 1;\nendmodule\n")


def _assume_unchanged_edit(root: Path) -> None:
    _run_git(root, "update-index", "--assume-unchanged", "a1.txt")
    (root / "a1.txt").write_bytes(b"LOCAL EDIT THAT MUST SURVIVE\n")


def _untracked_note(root: Path) -> None:
    (root / "notes.txt").write_bytes(b"keep me\n")


def _foreign_hardlink(root: Path) -> None:
    os.link(root / "a.v", root / "untracked-mirror.v")


_PNG = b"\x89PNG\r\n\x1a\n\x00binary\r\npayload\x00"
_BAT = {".gitattributes": b"*.bat -text\n", "run.bat": b"@echo off\r\n"}


@pytest.mark.parametrize(
    ("files", "crlf", "setup", "kept"),
    [
        pytest.param({"image.png": _PNG}, (), None, {"image.png": _PNG}, id="binary"),
        pytest.param(
            {".gitattributes": b"*.bat -text\n"},
            (),
            None,
            {".gitattributes": b"*.bat -text\n"},
            id="tracked-gitattributes",
        ),
        pytest.param(_BAT, (), None, {"run.bat": b"@echo off\r\n"}, id="minus-text"),
        pytest.param({}, (), _untracked_note, {"notes.txt": b"keep me\n"}, id="untracked"),
        pytest.param(
            {"local.v": b"module local;\nendmodule\n"},
            (),
            _skip_worktree_edit,
            {"local.v": b"module local;\n  localparam int KEEP = 1;\nendmodule\n"},
            id="skip-worktree",
        ),
        pytest.param(
            {"a[1].txt": b"candidate\n", "a1.txt": b"neighbor\n"},
            ("a[1].txt", "a1.txt"),
            _assume_unchanged_edit,
            {"a1.txt": b"LOCAL EDIT THAT MUST SURVIVE\n"},
            id="literal-names",
        ),
        pytest.param(
            {"bad-\udcff.txt": b"alpha\nbeta\n"},
            ("bad-\udcff.txt",),
            None,
            {},
            id="non-utf8-name",
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows filenames are Unicode"),
        ),
        pytest.param(
            {},
            (),
            _foreign_hardlink,
            {"untracked-mirror.v": CRLF},
            id="foreign-hardlink",
            marks=_NEEDS_HARDLINKS,
        ),
    ],
)
def test_repair_normalizes_crlf_and_keeps_other_bytes(tmp_path, files, crlf, setup, kept):
    _repo(tmp_path, "true", {"a.v": LF, **files}, crlf=("a.v", *crlf))
    if setup is not None:
        setup(tmp_path)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    for name in ("a.v", *crlf):
        assert b"\r" not in (tmp_path / name).read_bytes()
    for name, data in kept.items():
        assert (tmp_path / name).read_bytes() == data
    assert _tracked_clean(tmp_path)


def test_unreadable_eol_scan_never_reports_container_safe(tmp_path, monkeypatch):
    _repo(tmp_path, "false", {"a.v": LF})
    real_run = subprocess.run

    def run(args: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess[Any]:
        if "--eol" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"index unreadable")
        return real_run(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)

    results = _init(tmp_path, monkeypatch)

    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "err",
        "EOL scan unreadable",
    )


# Usually ~3 s on Windows CI, but runner contention has stretched it to 48.6 s;
# the budget keeps the timeout-headroom guard's 3x margin over that peak.
@pytest.mark.timeout(150)
def test_failed_smudge_filter_leaves_original_file_present(tmp_path):
    _repo(tmp_path, "true", {"a.v": LF, ".gitattributes": b"a.v filter=fail\n"}, crlf=("a.v",))
    for key, value in (
        ("clean", "cat"),
        ("smudge", "git booley-filter-must-fail"),
        ("required", "true"),
    ):
        _run_git(tmp_path, "config", f"filter.fail.{key}", value)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == CRLF


@_NEEDS_HARDLINKS
def test_init_releases_own_guidance_hardlinks_for_normalization(tmp_path, monkeypatch, capsys):
    data = _guidance_project(tmp_path)
    canonical = data / "AGENTS.md"
    foreign = tmp_path / "notes-mirror.md"
    os.link(canonical, foreign)

    _init(tmp_path, monkeypatch, check_only=True)

    assert canonical.read_bytes() == b"guidance\r\n"
    assert all((tmp_path / name).samefile(canonical) for name in ("AGENTS.md", "CLAUDE.md"))
    capsys.readouterr()
    # The preflight plan saw the links; the repair releases them and must not reuse it.
    plan = plan_guidance_links(tmp_path, data)

    results = _init(tmp_path, monkeypatch, guidance_plan=plan)

    assert [(results[n].status, n) for n in ("line_endings", "guidance_links")] == [
        ("ok", "line_endings"),
        ("ok", "guidance_links"),
    ]
    assert canonical.read_bytes() == GUIDANCE
    assert all((tmp_path / name).samefile(canonical) for name in ("AGENTS.md", "CLAUDE.md"))
    assert foreign.read_bytes() == b"guidance\r\n"
    assert not foreign.samefile(canonical)
    assert "released guidance hardlink CLAUDE.md" in capsys.readouterr().out
    final = _init(tmp_path, monkeypatch, check_only=True)
    assert (final["line_endings"].status, final["guidance_links"].status) == ("ok", "ok")


@_NEEDS_HARDLINKS
def test_partial_release_failure_keeps_the_locked_links_bytes_and_reports_it(
    tmp_path, monkeypatch
):
    data = _guidance_project(tmp_path)
    canonical = data / "AGENTS.md"
    plan = plan_guidance_links(tmp_path, data)
    original_unlink = Path.unlink

    def locked_claude(path: Path, missing_ok: bool = False) -> None:
        if path == tmp_path / "CLAUDE.md":
            raise PermissionError("guidance is open in another application")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_claude)

    results = _init(tmp_path, monkeypatch, guidance_plan=plan)

    # A Booley link that could not be released holds the repair back: replacing the
    # canonical file would strand that link on stale bytes and block the next run.
    assert (results["line_endings"].status, results["line_endings"].detail) == (
        "err",
        "guidance hardlink not released",
    )
    assert results["guidance_links"].status == "ok"
    assert canonical.read_bytes() == b"guidance\r\n"
    assert all((tmp_path / name).samefile(canonical) for name in ("AGENTS.md", "CLAUDE.md"))


@_NEEDS_HARDLINKS
def test_linked_project_data_guidance_is_repaired(tmp_path, monkeypatch):
    original = _guidance_project(tmp_path)
    data = tmp_path / "project-data"
    original.rename(data)
    symlink_or_skip(original, data, target_is_directory=True)
    plan = plan_guidance_links(tmp_path, original)
    baseline = sample_worktree_cleanliness(tmp_path, original)

    results = _init(tmp_path, monkeypatch, guidance_plan=plan, line_ending_baseline=baseline)

    assert (results["line_endings"].status, results["guidance_links"].status) == ("ok", "ok")
    assert (data / "AGENTS.md").read_bytes() == GUIDANCE
    assert all(
        (tmp_path / name).samefile(data / "AGENTS.md") for name in ("AGENTS.md", "CLAUDE.md")
    )
    final = _init(tmp_path, monkeypatch, check_only=True)
    assert (final["line_endings"].status, final["guidance_links"].status) == ("ok", "ok")


@_NEEDS_HARDLINKS
def test_release_keeps_links_when_canonical_needs_no_normalization(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, None)
    data = _repo(tmp_path / ".booley", None, {"AGENTS.md": GUIDANCE})
    os.link(data / "AGENTS.md", tmp_path / "AGENTS.md")

    _init(tmp_path, monkeypatch)

    assert "released guidance hardlink" not in capsys.readouterr().out
    assert (tmp_path / "AGENTS.md").samefile(data / "AGENTS.md")


@_NEEDS_HARDLINKS
def test_release_warns_when_inspection_fails(tmp_path, monkeypatch, capsys):
    _guidance_project(tmp_path)

    def unreadable(*_args: object) -> tuple[Path, ...]:
        raise OSError("index unreadable")

    # Private seam: the guidance release asks this helper for pending paths; the real
    # inspection has no input that makes the index unreadable only for that call.
    monkeypatch.setattr(init_cmd, "pending_normalization_paths", unreadable)

    _init(tmp_path, monkeypatch)

    out = capsys.readouterr().out
    assert "could not release guidance hardlinks" in out
    assert "released guidance hardlink" not in out
    assert (tmp_path / "AGENTS.md").exists()


class TestDetachGuidanceHardlinks:
    """Only Booley's untracked root hardlinks to the canonical file are released."""

    @staticmethod
    def _canonical(tmp_path: Path) -> Path:
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        canonical = booley_dir / "AGENTS.md"
        canonical.write_bytes(b"guidance\n")
        return canonical

    def test_missing_or_unlinked_canonical_detaches_nothing(self, tmp_path: Path) -> None:
        from booley.harness.setup.guidance_links import detach_guidance_hardlinks

        booley_dir = tmp_path / ".booley"
        assert detach_guidance_hardlinks(tmp_path, booley_dir) == ()
        self._canonical(tmp_path)
        (tmp_path / "AGENTS.md").write_bytes(b"guidance\n")  # a copy, not a link
        assert detach_guidance_hardlinks(tmp_path, booley_dir) == ()
        assert (tmp_path / "AGENTS.md").is_file()

    def test_unstatable_canonical_detaches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.harness.setup import guidance_links

        canonical = self._canonical(tmp_path)
        original_stat = Path.stat

        def failing_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
            if path == canonical:
                raise PermissionError("denied")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", lambda _path: True)
        monkeypatch.setattr(Path, "stat", failing_stat)
        assert guidance_links.detach_guidance_hardlinks(tmp_path, canonical.parent) == ()

    @pytest.mark.parametrize("failure", ["exit", "timeout", "os-error"])
    def test_unreadable_tracking_preserves_root_hardlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        from booley.harness.setup import guidance_links

        _git_init(tmp_path)
        canonical = self._canonical(tmp_path)
        for name in ("AGENTS.md", "CLAUDE.md"):
            os.link(canonical, tmp_path / name)
        _run_git(tmp_path, "add", "-f", "AGENTS.md", "CLAUDE.md")
        original_run = subprocess.run

        def unreadable(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "ls-files" not in args:
                return original_run(args, **kwargs)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 10)
            if failure == "os-error":
                raise OSError("Git unavailable")
            return subprocess.CompletedProcess(args, 128, "", "index unreadable")

        monkeypatch.setattr(subprocess, "run", unreadable)
        assert guidance_links.detach_guidance_hardlinks(tmp_path, canonical.parent) == ()
        for name in ("AGENTS.md", "CLAUDE.md"):
            assert (tmp_path / name).samefile(canonical)

    @pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
    def test_skips_symlinks_tracked_entries_and_unreadable_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.harness.setup import guidance_links

        _git_init(tmp_path)
        canonical = self._canonical(tmp_path)
        os.link(canonical, tmp_path / "AGENTS.md")
        _run_git(tmp_path, "add", "-f", "AGENTS.md")
        try:
            (tmp_path / "CLAUDE.md").symlink_to(canonical)
        except OSError:
            pytest.skip("symlinks unavailable")

        assert guidance_links.detach_guidance_hardlinks(tmp_path, canonical.parent) == ()
        assert (tmp_path / "AGENTS.md").samefile(canonical)
        assert (tmp_path / "CLAUDE.md").is_symlink()

        (tmp_path / "CLAUDE.md").unlink()
        os.link(canonical, tmp_path / "CLAUDE.md")

        def vanished(_path: Path, _other: Path) -> bool:
            raise FileNotFoundError("raced away")

        monkeypatch.setattr(Path, "samefile", vanished)
        assert guidance_links.detach_guidance_hardlinks(tmp_path, canonical.parent) == ()
        assert (tmp_path / "CLAUDE.md").exists()


@pytest.mark.parametrize("locked", [False, True])
def test_link_policy_ignores_ambient_git_repository_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool
):
    # Private seam: a whole Project Initialization also runs the prune guard, which
    # follows ambient GIT_DIR, so only the link-policy step shows this isolation.
    root, foreign = tmp_path / "root", tmp_path / "foreign"
    data = _policy_project(root)
    _git_init(foreign)
    _run_git(root, "config", WORKTREE_RELATIVE_KEY, "false")
    _run_git(foreign, "config", WORKTREE_RELATIVE_KEY, "true")
    if locked:
        (data / ".git" / "config.lock").write_bytes(b"")
    foreign_before = (foreign / ".git" / "config").read_bytes()
    ctx = _ctx(root)

    with monkeypatch.context() as ambient:
        ambient.setenv("GIT_DIR", str(foreign / ".git"))
        ambient.setenv("GIT_COMMON_DIR", str(foreign / ".git"))
        ambient.setenv("GIT_WORK_TREE", str(foreign))
        git_hooks._step_worktree_link_policy(
            ctx, host_git_version=(2, 53, 0), sandbox_git_version=(2, 53, 0), booley_dir=data
        )

    assert ctx.results[-1].status == ("warn" if locked else "ok")
    assert git_hooks.read_local_config(root, WORKTREE_RELATIVE_KEY) == (
        "false" if locked else "true"
    )
    assert git_hooks.read_local_config(data, WORKTREE_RELATIVE_KEY) == (None if locked else "true")
    assert (foreign / ".git" / "config").read_bytes() == foreign_before


def test_link_policy_set_by_init_is_read_from_a_linked_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, linked = tmp_path / "root", tmp_path / "linked"
    _policy_project(root, data_repository=False)
    commit = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
    _run_git(root, *commit, "commit", "--allow-empty", "-m", "seed")
    _run_git(root, "worktree", "add", "-b", "linked", str(linked))

    _initialize(root, monkeypatch)

    assert git_hooks.read_local_config(linked, WORKTREE_RELATIVE_KEY) == "true"


@pytest.mark.parametrize("failure", ["empty", "resolve"])
def test_policy_discovery_rejects_missing_repository_identity(tmp_path, monkeypatch, failure):
    root = tmp_path / "root"
    data = root / ".booley"
    _git_init(root)
    _git_init(data)
    if failure == "empty":
        monkeypatch.chdir(data)
        monkeypatch.setattr(
            git_hooks.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
        )
    else:
        real_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == data:
                raise OSError("repository identity unreadable")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    assert git_hooks.worktree_policy_repositories(root, booley_dir=data) == (root,)


@pytest.mark.parametrize("stealth", [False, True])
def test_init_writes_no_attributes_file(tmp_path, monkeypatch, capsys, stealth):
    _repo(tmp_path, "true")
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text(f"[stealth]\nenabled = {str(stealth).lower()}\n")
    attribute_files = (tmp_path / ".gitattributes", tmp_path / ".git/info/attributes")

    for check_only, autocrlf in ((True, "true"), (False, "false")):
        results = _initialize(tmp_path, monkeypatch, check_only=check_only)

        assert results["line_endings"].status == ("warn" if check_only else "ok")
        out = capsys.readouterr().out
        assert "text=auto" not in out and "added '" not in out
        assert git_hooks.read_local_config(tmp_path, "core.autocrlf") == autocrlf
        assert not any(path.exists() for path in attribute_files)


def test_completed_normalization_of_no_files_renders_nothing(tmp_path, monkeypatch, capsys):
    done = ActionResult(ActionKind.NORMALIZE_FILES, ActionState.COMPLETED, count=0)
    _repo(tmp_path, "false")

    _initialize_with_report(tmp_path, monkeypatch, _report(LineEndingStatus.SAFE, actions=(done,)))

    out = capsys.readouterr().out
    assert "0 tracked file(s)" not in out
    assert "normalized 0" not in out


_PIN_DONE = ActionResult(ActionKind.PIN_AUTOCRLF, ActionState.COMPLETED)
_NORMALIZED = ActionResult(ActionKind.NORMALIZE_FILES, ActionState.COMPLETED)
_UNSAFE_DETAILS = {
    ObservationCode.AUTOCRLF_UNREADABLE: "autocrlf unreadable",
    ObservationCode.LOCAL_AUTOCRLF_UNREADABLE: "local autocrlf unreadable",
    ObservationCode.EOL_SCAN_UNREADABLE: "EOL scan unreadable",
    ObservationCode.STATUS_UNREADABLE: "status comparison unreadable",
    ObservationCode.AUTOCRLF_EFFECTIVE_TRUE: "autocrlf policy unsafe",
    ObservationCode.AUTOCRLF_NOT_PINNED: "autocrlf policy unsafe",
    ObservationCode.CRLF_MISMATCH: "CRLF working tree",
    ObservationCode.STALE_INDEX: "stale index metadata",
}


def _report(
    status: LineEndingStatus,
    observations: tuple[Observation, ...] = (),
    actions: tuple[ActionResult, ...] = (),
) -> LineEndingReport:
    repository = LineEndingRepository("project-checkout", Path("/repo"))
    return LineEndingReport(
        status, (RepositoryLineEndingReport(repository, status, observations, actions),), ()
    )


def _refused(reason: LineEndingRefusalReason, detail: str) -> ActionResult:
    return ActionResult(
        ActionKind.NORMALIZE_FILES, ActionState.REFUSED, count=1, detail=detail, reason=reason
    )


_UNSAFE, _SAFE = LineEndingStatus.UNSAFE, LineEndingStatus.SAFE
_SUMMARY_CASES = [
    # A completed pin must not mask the observation (the precedence #4857a6235 restored);
    # indexing by every code makes a new code without a detail fail collection.
    *(
        pytest.param(
            _UNSAFE,
            (Observation(code, detail="raw detail sentinel"),),
            pin,
            _UNSAFE_DETAILS[code],
            id=f"{code.value}-{name}",
        )
        for code in ObservationCode
        for name, pin in (("no-pin", ()), ("pin-done", (_PIN_DONE,)))
    ),
    pytest.param(
        _UNSAFE,
        (),
        (_refused(LineEndingRefusalReason.DIRTY_TREE, "Das Arbeitsverzeichnis ist geändert"),),
        "dirty tree",
        id="dirty-tree-reason",
    ),
    pytest.param(
        _UNSAFE,
        (),
        (
            _refused(
                LineEndingRefusalReason.NOT_LINE_ENDING_ONLY,
                "working tree has changes of its own kind",
            ),
        ),
        "working tree has changes of its own kind",
        id="own-refusal-detail",
    ),
    pytest.param(
        _UNSAFE,
        (),
        (
            ActionResult(ActionKind.REFRESH_INDEX, ActionState.COMPLETED),
            ActionResult(ActionKind.PIN_AUTOCRLF, ActionState.REFUSED, detail="pin unsafe"),
        ),
        "pin unsafe",
        id="refused-pin",
    ),
    pytest.param(_SAFE, (), (), "no CRLF", id="no-crlf"),
    pytest.param(_SAFE, (), (_NORMALIZED,), "normalized", id="normalized"),
    pytest.param(
        _UNSAFE,
        (Observation(ObservationCode.EOL_SCAN_UNREADABLE),),
        (_NORMALIZED,),
        "EOL verification unreadable",
        id="eol-verification-unreadable",
    ),
    pytest.param(_UNSAFE, (), (), "unsafe line-ending state", id="unsafe-without-observation"),
]


@pytest.mark.parametrize(("status", "observations", "actions", "detail"), _SUMMARY_CASES)
def test_line_ending_summary_detail(tmp_path, monkeypatch, status, observations, actions, detail):
    _repo(tmp_path, "false")

    results = _initialize_with_report(
        tmp_path, monkeypatch, _report(status, observations, actions)
    )

    assert results["line_endings"].detail == detail


def _tracking_git(root: Path, *args: str) -> str:
    return run_bounded_child(["git", "-C", str(root), *args], check=True).stdout


def _tracked_bundle_fixture(tmp_path, monkeypatch, topology, staged):
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "root with spaces"
    root.mkdir()
    _git_init(root)
    data = root / ".booley"
    data.mkdir(mode=0o700)
    owner = data if topology == "standalone" else root
    if topology == "standalone":
        _git_init(owner)
    for repo in {root, owner}:
        _tracking_git(repo, "config", "user.name", "Fixture")
        _tracking_git(repo, "config", "user.email", "fixture@example.invalid")
    (data / ".gitignore").write_text(PROJECT_GITIGNORE)
    reset_cache()
    step_project_git_hooks(InitContext(project_root=root))
    bundle = data / ".managed/project-git-hooks.pyz"
    _tracking_git(owner, "add", "-f", str(bundle))
    if not staged:
        _tracking_git(owner, "commit", "-qm", "legacy tracked bundle")
    return root, owner, bundle


@pytest.mark.parametrize("topology", ["standalone", "nested"])
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("outdated", [False, True])
@pytest.mark.parametrize("check_only", [False, True])
def test_tracked_bundle_reports_untrack_command(
    tmp_path, monkeypatch, capsys, topology, staged, outdated, check_only
):
    root, owner, bundle = _tracked_bundle_fixture(tmp_path, monkeypatch, topology, staged)
    if outdated:
        bundle.write_bytes(b"outdated bundle")
    index = _tracking_git(owner, "ls-files", "--stage", "-z")
    diff = _tracking_git(owner, "diff", "--cached", "--binary")
    before = bundle.read_bytes()
    capsys.readouterr()
    ctx = InitContext(project_root=root, check_only=check_only)
    step_project_git_hooks(ctx)
    relative = bundle.relative_to(owner).as_posix()
    command = shlex.join(["git", "-C", str(owner.resolve()), "rm", "--cached", "--", relative])
    assert command in capsys.readouterr().out
    # `booley init` exits 1 only for a check-mode `warn`, which an outdated bundle yields.
    expected = {
        (False, False): "current",
        (False, True): "ok",
        (True, False): "current",
        (True, True): "warn",
    }[check_only, outdated]
    assert [(r.name, r.status) for r in ctx.results] == [
        ("project_git_hook_tracking", "skip"),
        ("project_git_hooks", expected),
    ]
    detail = ctx.results[0].detail
    assert command in detail
    assert "Commit the intentional removal" in detail
    assert "rerun init" in detail
    assert _tracking_git(owner, "ls-files", "--stage", "-z") == index
    assert _tracking_git(owner, "diff", "--cached", "--binary") == diff
    if check_only:
        assert bundle.read_bytes() == before
    reset_cache()


def _paired_hook_fixture(tmp_path, monkeypatch):
    from booley.harness import worktree_cmd
    from tests.harness.test_worktree_cmd import _new, python_outer, versioned_project

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    root = versioned_project.__wrapped__(tmp_path)
    # Default runtime worktrees lie inside Project state; the existing push
    # guard rejects their physical path independently of bundle resolution.
    monkeypatch.setattr(worktree_cmd, "worktree_path", lambda _root, name: tmp_path / name)
    python_outer.__wrapped__(monkeypatch)
    data = root / ".booley"
    for owner in (root, data):
        _tracking_git(owner, "config", "user.name", "Fixture")
        _tracking_git(owner, "config", "user.email", "fixture@example.invalid")
    primary_policy = data / "booley.toml"
    primary_policy.write_text(
        "[stealth]\nenabled=true\nbanned_words=['primaryphrase']\n"
        "[agent.git]\nname='Fixture'\nemail='fixture@example.invalid'\n"
    )
    _tracking_git(data, "add", "booley.toml")
    _tracking_git(data, "commit", "-qm", "authored primary policy")
    reset_cache()
    ctx = InitContext(project_root=root)
    step_project_git_hooks(ctx)
    assert ctx.results[-1].status == "ok"
    assert _new("policy-fallback", root) == 0
    linked = worktree_cmd.worktree_path(root, "policy-fallback")
    paired_data = linked / ".booley"
    assert (paired_data / ".git").is_file()
    assert not (paired_data / ".managed/project-git-hooks.pyz").exists()
    assert not (data / ".git/hooks/commit-msg").exists()
    (paired_data / "booley.toml").write_text(
        "[stealth]\nenabled=true\nbanned_words=['pairedphrase']\n"
    )
    return root, linked


@pytest.mark.parametrize("checkout", ["main", "paired"])
def test_paired_hooks_use_main_bundle_without_local_copy(tmp_path, monkeypatch, checkout):
    root, linked = _paired_hook_fixture(tmp_path, monkeypatch)
    repo = root if checkout == "main" else linked
    for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
        _tracking_git(repo, "config", key, value)
    (repo / "change.v").write_text("module changed; endmodule\n")
    _tracking_git(repo, "add", "change.v")
    head = _tracking_git(repo, "rev-parse", "HEAD")
    rejected = run_bounded_child(
        ["git", "-C", str(repo), "commit", "-qm", "fix(core): primaryphrase pairedphrase"],
    )
    # Each checkout's own policy rejects its own phrase and names only that one.
    present, absent = (
        ("primaryphrase", "pairedphrase")
        if checkout == "main"
        else ("pairedphrase", "primaryphrase")
    )
    assert rejected.returncode != 0
    assert f"'{present}' in the subject" in rejected.stderr
    assert f"'{absent}'" not in rejected.stderr
    assert _tracking_git(repo, "rev-parse", "HEAD") == head
    _tracking_git(repo, "commit", "-qm", "fix(core): ordinary change")
    remote = tmp_path / "local-remote.git"
    _tracking_git(tmp_path, "init", "--bare", "-q", str(remote))
    banned = "primaryphrase" if checkout == "main" else "pairedphrase"
    _tracking_git(repo, "commit", "--amend", "--no-verify", "-qm", f"fix(core): {banned}")
    refused = run_bounded_child(
        ["git", "-C", str(repo), "push", str(remote), "HEAD:refs/heads/policy-test"]
    )
    assert refused.returncode != 0, refused.stderr
    assert "push blocked by leak-guard pre-push hook" in refused.stderr
    assert not _tracking_git(remote, "for-each-ref", "--format=%(refname)")
    _tracking_git(repo, "commit", "--amend", "-qm", "fix(core): safe content")
    _tracking_git(repo, "push", str(remote), "HEAD:refs/heads/policy-test")
    assert _tracking_git(remote, "rev-parse", "refs/heads/policy-test") == _tracking_git(
        repo, "rev-parse", "HEAD"
    )
    reset_cache()


@pytest.mark.parametrize("location", ["fresh", "external", "untracked"])
def test_tracked_bundle_advisory_absent_without_index_entry(
    tmp_path, monkeypatch, capsys, location
):
    root = tmp_path / "root"
    root.mkdir()
    _git_init(root)
    data = tmp_path / "external" if location == "external" else root / ".booley"
    if location != "fresh":
        data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    ctx = InitContext(project_root=root, check_only=True)
    reconcile.step_project_git_hooks(ctx)
    output = capsys.readouterr().out
    assert "rm --cached" not in output
    assert "already tracked" not in output
    tracking = [result for result in ctx.results if result.name == "project_git_hook_tracking"]
    if tracking:
        assert location == "external"
        assert len(tracking) == 1
        assert tracking[0].status == "skip"
        assert "Stopping at filesystem boundary" in tracking[0].detail
    assert ctx.results[-1].status == "warn"
    assert not (data / ".managed").exists()
    reset_cache()


@pytest.mark.parametrize("failure", ["timeout", "empty-owner", "index-error", "owner-error"])
def test_tracked_bundle_unexpected_probe_diagnostic_continues(
    tmp_path, monkeypatch, capsys, failure
):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    _git_init(root)
    (root / ".booley").mkdir()
    reset_cache()
    original = reconcile.subprocess.run

    def fail_probe(args, **kwargs):
        if "--show-toplevel" in args and failure == "timeout":
            raise subprocess.TimeoutExpired(args, 10)
        if "--show-toplevel" in args and failure == "empty-owner":
            return subprocess.CompletedProcess(args, 0, "", "")
        if "--show-toplevel" in args and failure == "owner-error":
            return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership")
        if "ls-files" in args and failure == "index-error":
            return subprocess.CompletedProcess(args, 128, "", "fatal: corrupt index")
        return original(args, **kwargs)

    monkeypatch.setattr(reconcile.subprocess, "run", fail_probe)
    ctx = InitContext(project_root=root)
    reconcile.step_project_git_hooks(ctx)
    output = capsys.readouterr().out
    assert "Could not inspect" in output
    assert ("dubious ownership" in output) == (failure == "owner-error")
    assert ctx.results[-2].name == "project_git_hook_tracking"
    assert ctx.results[-2].status == "skip"
    assert ctx.results[-1].status == "ok"
    assert (root / ".booley/.managed/project-git-hooks.pyz").is_file()
    reset_cache()


@pytest.mark.parametrize("selectors", ["ceiling", "all"])
def test_tracked_bundle_advisory_ignores_ambient_git_selectors(
    tmp_path, monkeypatch, capsys, selectors
):
    root, owner, bundle = _tracked_bundle_fixture(tmp_path, monkeypatch, "nested", False)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git_init(foreign)
    overrides = {"GIT_CEILING_DIRECTORIES": str(owner)}
    if selectors == "all":
        overrides.update(
            GIT_DIR=str(foreign / ".git"),
            GIT_COMMON_DIR=str(foreign / ".git"),
            GIT_WORK_TREE=str(foreign),
            GIT_INDEX_FILE=str(foreign / ".git/index"),
            GIT_OBJECT_DIRECTORY=str(foreign / ".git/objects"),
            GIT_ALTERNATE_OBJECT_DIRECTORIES=str(foreign / ".git/objects"),
            GIT_PREFIX="foreign/",
            GIT_DISCOVERY_ACROSS_FILESYSTEM="0",
            GIT_ATTR_SOURCE="HEAD",
        )
    (root / ".git" / "hooks" / "commit-msg").unlink()
    capsys.readouterr()
    ctx = InitContext(project_root=root)
    with monkeypatch.context() as ambient:
        for name, value in overrides.items():
            ambient.setenv(name, value)
        step_project_git_hooks(ctx)
    output = capsys.readouterr().out
    relative = bundle.relative_to(owner).as_posix()
    command = shlex.join(["git", "-C", str(owner), "rm", "--cached", "--", relative])
    assert command in output
    assert "Could not inspect" not in output
    assert [(r.name, r.status) for r in ctx.results] == [
        ("project_git_hook_tracking", "skip"),
        ("project_git_hooks", "ok"),
    ]
    assert (root / ".git" / "hooks" / "commit-msg").is_file()
    assert not list((foreign / ".git" / "hooks").glob("commit-msg"))
    assert not list((foreign / ".git" / "hooks").glob("pre-push"))
    assert _tracking_git(owner, "ls-files", "--error-unmatch", str(bundle))
    reset_cache()


def test_tracked_bundle_broken_gitfile_reports_diagnostic(tmp_path, monkeypatch, capsys):
    root, owner, bundle = _tracked_bundle_fixture(tmp_path, monkeypatch, "nested", False)
    (bundle.parent.parent / ".git").write_text(f"gitdir: {tmp_path / 'missing-git-dir'}\n")
    index = _tracking_git(owner, "ls-files", "--stage", "-z")
    capsys.readouterr()
    ctx = InitContext(project_root=root, check_only=True)
    step_project_git_hooks(ctx)
    assert "Could not inspect Project Git-hook bundle tracking" in capsys.readouterr().out
    assert ctx.results[-2].name == "project_git_hook_tracking"
    assert ctx.results[-2].status == "skip"
    assert ctx.results[-1].detail == "current"
    assert _tracking_git(owner, "ls-files", "--stage", "-z") == index
    assert bundle.is_file()
    reset_cache()
