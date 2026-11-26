"""Selected worktree storage and Git metadata capability checks."""

from pathlib import Path

import pytest

from booley.runtime.booley_dir import reset_cache
from booley.runtime.worktree_paths import worktree_creation_config
from tests.conftest import symlink_or_skip
from tests.git_command_fake import answer_git_version

ABSOLUTE = ("-c", "worktree.useRelativePaths=false")
SUPPORTED = "git version 2.48.0\n"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project Root whose selected Booley Directory is its own ``.booley``."""
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_DIR", str(root / ".booley"))
    # Another test in this worker may have cached a different Booley Directory.
    reset_cache()
    return root


@pytest.mark.parametrize(
    ("reply", "relative"),
    [
        (SUPPORTED, True),
        ("git version 2.47.9\n", False),
        ("git version unknown\n", False),
        (OSError("Git unavailable"), False),
    ],
    ids=["supported", "unsupported", "unparsable", "unavailable"],
)
def test_relative_metadata_needs_git_2_48(
    root: Path, monkeypatch: pytest.MonkeyPatch, reply: str | OSError, relative: bool
) -> None:
    answer_git_version(monkeypatch, reply)

    assert worktree_creation_config(root) == (() if relative else ABSOLUTE)


def test_linked_root_or_external_state_keeps_absolute_metadata(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_git_version(monkeypatch, SUPPORTED)
    (root / ".git").write_text("gitdir: /linked/owner\n")
    assert worktree_creation_config(root) == ABSOLUTE

    (root / ".git").unlink()
    (root / ".booley").rmdir()
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    reset_cache()
    assert worktree_creation_config(root) == ABSOLUTE


@pytest.mark.parametrize("inside", [False, True], ids=["target_outside", "target_inside"])
def test_symlinked_booley_dir_entry_uses_absolute_worktree_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    """Git writes relative metadata from physical paths, so only an in-root target is portable."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / "control" if inside else tmp_path / "booley-state"
    target.mkdir()
    symlink_or_skip(root / ".booley", target)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    answer_git_version(monkeypatch, SUPPORTED)
    reset_cache()

    assert worktree_creation_config(root) == (() if inside else ABSOLUTE)
