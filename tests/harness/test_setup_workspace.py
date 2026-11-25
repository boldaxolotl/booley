"""Tests for the setup stage's workspace preparation -- worktree reuse, branch logic."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.subprocess_budget import run_bounded_child


@pytest.fixture(autouse=True)
def _select_worktree_state(tmp_path: Path, monkeypatch):
    from booley.core.booley_dir import reset_cache

    state = tmp_path / ".booley"
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


def _add_worktree(project_root: Path, name: str) -> subprocess.CompletedProcess[str]:
    """Add a plain Git worktree of *project_root* under its Booley Directory."""
    worktree = project_root / ".booley" / "worktrees" / name
    return _git(project_root, "worktree", "add", "-q", "-b", name, str(worktree), "HEAD")


def _create_and_set_up(project_root: Path, name: str) -> subprocess.CompletedProcess[str]:
    """Add a worktree, then apply the setup `booley worktree prepare` runs."""
    from booley.runtime.worktree_setup import configure_worktree_git, copy_root_fusesoc_ignore

    result = _add_worktree(project_root, name)
    if result.returncode == 0:
        worktree = project_root / ".booley" / "worktrees" / name
        configure_worktree_git(project_root, worktree, booley_dir=project_root / ".booley")
        copy_root_fusesoc_ignore(project_root, worktree)
    return result


class TestWorktreeSetup:
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
        result = _create_and_set_up(project_root, "configured-identity")
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

        result = _create_and_set_up(project_root, "no-legacy-identity")

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / "no-legacy-identity"
        assert _git(worktree, "config", "user.name").stdout.strip() == "Dev"
        assert _git(worktree, "config", "user.email").stdout.strip() == "dev@localhost"

    @pytest.mark.timeout(120)  # Windows CI: 37.4 s including fixtures; retain 3x headroom.
    def test_invalid_git_identity_fails_worktree_creation(self, tmp_path: Path):
        project_root, project_data = _submodule_project(tmp_path)
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["vendor/ip core"]\n\n[agent.git]\nname = 42\n',
            encoding="utf-8",
        )

        from booley.runtime.worktree_setup import WorktreeSetupError

        with pytest.raises(WorktreeSetupError, match=r"\[agent.git\] name must be a string"):
            _create_and_set_up(project_root, "invalid-identity")

    @pytest.mark.timeout(120)
    def test_handles_parent_core_worktree_from_docker(self, tmp_path: Path):
        """Host setup must survive a parent config polluted with /work."""
        from booley.runtime.worktree_setup import mark_fusesoc_ignore

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
        mark_fusesoc_ignore(project_data, paired=False)
        result = _create_and_set_up(project_root, slug)

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / slug
        assert worktree.is_dir()
        assert _git(worktree, "status", "--short").returncode == 0
        # The worktree carries a copy of the project's .core files; the script
        # must drop FUSESOC_IGNORE so FuseSoC's --cores-root scan can't let a
        # stale copy shadow the source at the Project Root (silently building wrong RTL).
        assert (project_data / "FUSESOC_IGNORE").is_file()
        assert (worktree / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "quarantine\n"
        settings = {
            "core.autocrlf": "false",
            "submodule.recurse": "false",
            "diff.ignoreSubmodules": "all",
            "status.submoduleSummary": "false",
        }
        for key, value in settings.items():
            assert _git(worktree, "config", "--worktree", "--get", key).stdout.strip() == value
        core_worktree = _git(worktree, "config", "--worktree", "--get", "core.worktree")
        assert not Path(core_worktree.stdout.strip()).is_absolute()

    def test_worktree_git_config_waits_for_the_parent_git_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from booley.core.file_lock import LockTimeoutError, nonblocking_file_lock
        from booley.runtime import worktree_setup

        project_root, project_data = _submodule_project(tmp_path)
        assert _add_worktree(project_root, "locked").returncode == 0
        worktree = project_data / "worktrees" / "locked"
        # The bound is the only way to observe the wait deterministically.
        monkeypatch.setattr(worktree_setup, "_PARENT_LOCK_TIMEOUT_S", 0.2)
        lock = project_data / "worktrees" / ".locks" / "_parent_git.lock"
        lock.parent.mkdir(parents=True)

        with lock.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
            with pytest.raises(LockTimeoutError):
                worktree_setup.configure_worktree_git(
                    project_root, worktree, booley_dir=project_data
                )
            assert _git(worktree, "config", "--worktree", "--get", "core.worktree").returncode != 0

        worktree_setup.configure_worktree_git(project_root, worktree, booley_dir=project_data)
        assert _git(worktree, "config", "--worktree", "--get", "core.worktree").stdout.strip()

    def test_relative_worktree_policy_refuses_an_absolute_pointer(self, tmp_path: Path):
        from booley.runtime.worktree_setup import WorktreeSetupError, configure_worktree_git

        project_root, project_data = _submodule_project(tmp_path)
        assert _add_worktree(project_root, "absolute").returncode == 0
        worktree = project_data / "worktrees" / "absolute"
        git_dir = _git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip()
        pointer = worktree / ".git"
        # Git hides the pointer on Windows, and a hidden file refuses to be reopened for writing.
        pointer.unlink()
        pointer.write_text(f"gitdir: {git_dir}\n", encoding="utf-8", newline="\n")
        _git(project_root, "config", "worktree.useRelativePaths", "true")

        with pytest.raises(WorktreeSetupError, match="non-portable metadata"):
            configure_worktree_git(project_root, worktree, booley_dir=project_data)


# ===========================================================================
# Helpers
# ===========================================================================


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return run_bounded_child(["git", *args], cwd=cwd)
