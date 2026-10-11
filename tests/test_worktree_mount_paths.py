"""Selected worktree storage and Git metadata capability checks."""

from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache


@pytest.fixture(autouse=True)
def _fresh_project_dir_cache():
    """Keep each test's Project selection independent of earlier tests in the worker."""
    reset_cache()
    yield
    reset_cache()


def test_old_git_repair_omits_unsupported_mode_flags(monkeypatch):
    from booley.runtime import worktree_paths, worktree_repair

    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: False)
    assert worktree_repair._metadata_flag(True) == ()
    assert worktree_repair._metadata_flag(False) == ()
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    assert worktree_repair._metadata_flag(True) == ("--relative-paths",)
    assert worktree_repair._metadata_flag(False) == ("--no-relative-paths",)


def test_unsupported_git_and_linked_root_use_explicit_creation_fallback(tmp_path, monkeypatch):
    from booley.runtime import worktree_paths

    root = tmp_path / "project"
    state = root / ".booley_project"
    state.mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: False)
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    assert worktree_paths.worktree_creation_config(root) == ()
    (root / ".git").write_text("gitdir: /linked/owner\n")
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )


def test_external_state_never_enables_relative_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import worktree_paths

    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    reset_cache()
    assert not worktree_paths.relative_worktree_paths(root)
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )


def test_git_version_transport_failure_keeps_absolute_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import worktree_paths

    def unavailable(*_args, **_kwargs) -> None:
        raise OSError("Git unavailable")

    monkeypatch.setattr(worktree_paths.subprocess, "run", unavailable)
    assert not worktree_paths._git_supports_relative_paths()
