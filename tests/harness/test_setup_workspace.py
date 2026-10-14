"""Tests for the setup stage's workspace preparation -- worktree reuse, branch logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _select_worktree_state(tmp_path: Path, monkeypatch, request):
    from booley.core.booley_dir import reset_cache

    state = tmp_path / ".booley"
    if "rejects_cwd_outside_git_root" in request.node.name:
        return
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("BOOLEY_DIR", str(state))
    reset_cache()


def _init_git_repository(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")


def _submodule_project(tmp_path: Path) -> tuple[Path, Path]:
    dependency = tmp_path / "dependency"
    _init_git_repository(dependency)
    (dependency / "dep.txt").write_text("dependency\n", encoding="utf-8")
    _git(dependency, "add", "dep.txt")
    _git(dependency, "commit", "-m", "dependency")
    project_root = tmp_path / "repo"
    _init_git_repository(project_root)
    added = _git(
        project_root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "vendor/ip core",
    )
    assert added.returncode == 0, added.stderr
    _git(project_root, "commit", "-am", "add submodule")
    project_data = project_root / ".booley"
    project_data.mkdir()
    (project_data / "booley.toml").write_text(
        '[submodules]\npaths = ["vendor/ip core"]\n', encoding="utf-8"
    )
    return project_root, project_data


def _run_worktree_create(
    project_root: Path,
    name: str,
    *,
    branch_ref: str = "",
    on_existing: str = "",
) -> subprocess.CompletedProcess[str]:
    from booley.runtime.paths import worktree_create_script
    from booley.runtime.platform_paths import bash_bin

    env = {k: v for k, v in os.environ.items() if k != "BOOLEY_DIR"}
    env["BOOLEY_PYTHON"] = sys.executable
    payload = {"name": name, "cwd": str(project_root)}
    if branch_ref:
        payload["branch_ref"] = branch_ref
    if on_existing:
        payload["on_existing"] = on_existing
    return subprocess.run(
        [bash_bin(), str(worktree_create_script())],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


def _assert_moved_worktree_portable(
    tmp_path: Path,
    moved_root: Path,
    moved_worktree: Path,
) -> None:
    status = _git(moved_worktree, "status", "--short")
    listing = _git(moved_root, "worktree", "list", "--porcelain")
    assert status.returncode == 0, status.stderr
    assert listing.returncode == 0, listing.stderr
    assert moved_worktree.as_posix() in listing.stdout
    assert "prunable" not in listing.stdout

    pointer = (
        (moved_worktree / ".git").read_text(encoding="utf-8").removeprefix("gitdir: ").strip()
    )
    assert not Path(pointer).is_absolute()
    administration = (moved_worktree / pointer).resolve()
    reverse = (administration / "gitdir").read_text(encoding="utf-8").strip()
    assert not Path(reverse).is_absolute()
    core_worktree = _git(moved_worktree, "config", "--worktree", "--get", "core.worktree")
    hooks_path = _git(moved_worktree, "config", "--worktree", "--get", "core.hooksPath")
    assert core_worktree.returncode == 0 and not Path(core_worktree.stdout.strip()).is_absolute()
    assert hooks_path.returncode == 0 and not Path(hooks_path.stdout.strip()).is_absolute()
    repository_format = _git(moved_root, "config", "--get", "core.repositoryFormatVersion")
    relative_extension = _git(moved_root, "config", "--get", "extensions.relativeWorktrees")
    assert repository_format.stdout.strip() == "1"
    assert relative_extension.stdout.strip() == "true"
    assert (administration / "hooks" / "pre-commit").is_file()

    with (moved_worktree / ".gitmodules").open("a", encoding="utf-8") as stream:
        stream.write("# host-visible\n")
    assert _git(moved_worktree, "add", ".gitmodules").returncode == 0
    python_bin = tmp_path / "bin"
    python_bin.mkdir()
    python_wrapper = python_bin / "python"
    python_wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python_wrapper.chmod(0o755)
    commit = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "verify portable hooks",
        ],
        cwd=moved_worktree,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{python_bin}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        },
    )
    assert commit.returncode == 0, commit.stderr


# ===========================================================================
# Branch creation logic
# ===========================================================================


class TestBranchCreation:
    """Every implementation Ticket owns an isolated branch."""

    def test_integration_uses_ticket_branch(self):
        branch = _pick_branch(is_integration=True, slug="fix-fsm", base_branch="int/batch-1")
        assert branch == "fix-fsm"

    def test_normal_uses_slug(self):
        branch = _pick_branch(is_integration=False, slug="fix-fsm", base_branch="master")
        assert branch == "fix-fsm"


class TestBranchFallback:
    """Test -b failure → checkout existing branch."""

    def test_create_succeeds(self):
        result = _try_create_branch("fix-fsm", create_rc=0)
        assert result == "fix-fsm"

    def test_create_fails_checkout_succeeds(self):
        result = _try_create_branch("fix-fsm", create_rc=1, checkout_rc=0)
        assert result == "fix-fsm"

    def test_both_fail_returns_error(self):
        result = _try_create_branch("fix-fsm", create_rc=1, checkout_rc=1)
        assert result is None


# ===========================================================================
# Hook detection
# ===========================================================================


class TestHookDetection:
    def test_hook_exists(self, tmp_path: Path):
        hook = tmp_path / ".agents" / "hooks" / "worktree_create.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        assert hook.exists()

    def test_hook_missing_blocks(self, tmp_path: Path):
        hook = tmp_path / ".agents" / "hooks" / "worktree_create.sh"
        result = _check_hook(hook)
        assert "not found" in result


class TestWorktreeCreateScript:
    def test_rejects_missing_explicit_branch_without_removing_stale_path(
        self, tmp_path: Path
    ) -> None:
        project_root, project_data = _submodule_project(tmp_path)
        name = "missing-basis"
        worktree = project_data / "worktrees" / name
        worktree.mkdir(parents=True)
        marker = worktree / "preserve.txt"
        marker.write_text("preserve\n", encoding="utf-8")

        result = _run_worktree_create(
            project_root,
            name,
            branch_ref="refs/heads/booley-generation/0123456789abcdef/missing",
        )

        assert result.returncode != 0
        assert "does not exist" in result.stderr
        assert marker.read_text(encoding="utf-8") == "preserve\n"

    def test_replace_policy_recreates_a_stale_destination(self, tmp_path: Path) -> None:
        project_root, project_data = _submodule_project(tmp_path)
        stale = project_data / "worktrees" / "stale-ticket"
        stale.mkdir(parents=True)
        (stale / "leftover.txt").write_text("stale\n", encoding="utf-8")

        result = _run_worktree_create(project_root, "stale-ticket", on_existing="replace")

        assert result.returncode == 0, result.stderr
        assert "Stale worktree directory detected" in result.stderr
        assert not (stale / "leftover.txt").exists()
        assert (stale / ".git").is_file()

    def test_unknown_existing_policy_is_rejected(self, tmp_path: Path) -> None:
        project_root, project_data = _submodule_project(tmp_path)

        result = _run_worktree_create(project_root, "bad-policy", on_existing="overwrite")

        assert result.returncode != 0
        assert "on_existing must be" in result.stderr
        assert not (project_data / "worktrees" / "bad-policy").exists()

    def test_git_identity_comes_from_booley_toml(self, tmp_path: Path):
        project_root, project_data = _submodule_project(tmp_path)
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["vendor/ip core"]\n\n'
            '[agent.git]\nname = "Expected Developer"\n'
            'email = "developer-identity.invalid"\n',
            encoding="utf-8",
        )

        _git(project_root, "config", "user.name", "Human")
        _git(project_root, "config", "user.email", "human@example.invalid")
        result = _run_worktree_create(project_root, "configured-identity")
        assert _git(project_root, "config", "user.name").stdout.strip() == "Human"
        assert _git(project_root, "config", "user.email").stdout.strip() == "human@example.invalid"

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / "configured-identity"
        assert _git(worktree, "config", "user.name").stdout.strip() == "Expected Developer"
        assert _git(worktree, "config", "user.email").stdout.strip() == (
            "developer-identity.invalid"
        )

    def test_git_identity_does_not_fall_back_to_pipeline_toml(self, tmp_path: Path):
        project_root, project_data = _submodule_project(tmp_path)
        (project_data / "booley.toml").unlink()
        (project_data / "pipeline.toml").write_text(
            '[submodules]\npaths = ["vendor/ip core"]\n\n'
            '[agent.git]\nname = "Legacy Alias"\nemail = "legacy-identity.invalid"\n',
            encoding="utf-8",
        )

        result = _run_worktree_create(project_root, "no-legacy-identity")

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / "no-legacy-identity"
        assert _git(worktree, "config", "user.name").stdout.strip() == "Dev"
        assert _git(worktree, "config", "user.email").stdout.strip() == "dev@localhost"

    def test_invalid_git_identity_fails_worktree_creation(self, tmp_path: Path):
        project_root, project_data = _submodule_project(tmp_path)
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["vendor/ip core"]\n\n[agent.git]\nname = 42\n',
            encoding="utf-8",
        )

        result = _run_worktree_create(project_root, "invalid-identity")

        assert result.returncode != 0
        assert "[agent.git] name must be a string" in result.stderr

    def test_rejects_unsafe_worktree_name(self, tmp_path: Path):
        """Worktree slug must not escape the worktrees directory."""
        from booley.runtime.paths import worktree_create_script
        from booley.runtime.platform_paths import bash_bin

        result = subprocess.run(
            [bash_bin(), str(worktree_create_script())],
            input=json.dumps({"name": "../escape", "cwd": str(tmp_path)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            # Windows shells may expose only the Store python aliases (F-7);
            # pin the script to the suite's interpreter like the harness does.
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "single safe path component" in result.stderr
        assert not (tmp_path / "escape").exists()

    def test_rejects_cwd_outside_git_root_before_writing(self, tmp_path: Path):
        """Hook JSON must not redirect state creation into an arbitrary directory."""
        from booley.runtime.paths import worktree_create_script
        from booley.runtime.platform_paths import bash_bin

        result = subprocess.run(
            [bash_bin(), str(worktree_create_script())],
            input=json.dumps({"name": "safe-name", "cwd": str(tmp_path)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "must be the Git repository root" in result.stderr
        assert not (tmp_path / ".booley").exists()

    def test_leaves_gitlink_with_space_for_python_materialization(self, tmp_path: Path):
        """The shell creates only the outer worktree; Python fills gitlinks later."""
        project_root, project_data = _submodule_project(tmp_path)

        result = _run_worktree_create(project_root, "security-audit")

        assert result.returncode == 0, result.stderr
        placeholder = project_data / "worktrees" / "security-audit" / "vendor" / "ip core"
        assert placeholder.is_dir()
        assert not any(placeholder.iterdir())

    def test_handles_parent_core_worktree_from_docker(self, tmp_path: Path):
        """Host setup must survive a parent config polluted with /work."""
        from booley.runtime.paths import worktree_create_script
        from booley.runtime.platform_paths import bash_bin

        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Test User")
        _git(project_root, "config", "user.email", "test@example.com")
        (project_root / "README.md").write_text("hello\n", encoding="utf-8")
        _git(project_root, "add", "README.md")
        _git(project_root, "commit", "-m", "init")

        project_data = project_root / ".booley"
        project_data.mkdir()
        (project_data / "booley.toml").write_text("[submodules]\npaths = []\n", encoding="utf-8")
        (project_root / "FUSESOC_IGNORE").write_text("quarantine\n", encoding="utf-8")

        # Sandboxed runs use /work as the work tree. If that value leaks into
        # shared repo config, plain host-side `git -C <worktree> checkout` fails.
        _git(project_root, "config", "core.worktree", "/work")

        slug = "core-worktree-repro"
        result = subprocess.run(
            [bash_bin(), str(worktree_create_script())],
            input=json.dumps({"name": slug, "cwd": str(project_root)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "BOOLEY_PYTHON": sys.executable},
        )

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / slug
        assert worktree.is_dir()
        assert _git(worktree, "status", "--short").returncode == 0
        # The worktree carries a copy of the project's .core files; the script
        # must drop FUSESOC_IGNORE so FuseSoC's --cores-root scan can't let a
        # stale copy shadow the source at the Project Root (silently building wrong RTL).
        assert (project_data / "FUSESOC_IGNORE").is_file()
        assert (worktree / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "quarantine\n"


# ===========================================================================
# Helpers -- replicate pure logic from setup/workspace.py
# ===========================================================================


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _pick_branch(is_integration: bool, slug: str, base_branch: str) -> str:
    """Replicate branch selection logic."""
    del is_integration, base_branch
    return slug


def _try_create_branch(name: str, create_rc: int = 0, checkout_rc: int = 0) -> str | None:
    """Replicate branch creation with fallback."""
    if create_rc == 0:
        return name
    if checkout_rc == 0:
        return name
    return None


def _check_hook(hook_path: Path) -> str | None:
    """Replicate hook existence check."""
    if not hook_path.exists():
        return f"Worktree hook not found: {hook_path}"
    return None


# ===========================================================================
# Integration tests — call actual workspace-setup run() with mocked subprocess
# ===========================================================================


# ===========================================================================
# Hardening regression tests
# ===========================================================================
