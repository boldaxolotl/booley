"""Waiver policy fingerprints under a symlinked Booley Directory entry (#2192)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from booley.goals.waiver_policy import waiver_policy_fingerprint
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir


def _linked_checkout(tmp_path: Path, checkout: str, anchor: str) -> tuple[Path, Path]:
    """Return (work_dir, anchor root) for a Project whose ``.booley`` is a symlink.

    The main checkout's Booley Directory is the entry itself; a Goal worktree under
    ``.booley/worktrees/`` carries its own real ``.booley`` snapshot.
    """
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    symlinked_booley_dir(project, tmp_path / "external-booley")
    work_dir = project
    booley_dir = project / ".booley"
    if checkout == "worktree":
        work_dir = project / ".booley" / "worktrees" / "goal-x"
        booley_dir = work_dir / ".booley"
        booley_dir.mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(
        f'[coverage.waivers]\nanchor = "{anchor}"\ndirectory = "cw"\n', encoding="utf-8"
    )
    return work_dir, booley_dir if anchor == "project_data_repository" else work_dir


@pytest.mark.parametrize(
    ("checkout", "anchor"),
    [
        ("main", "project_data_repository"),
        ("main", "rtl_repository"),
        ("worktree", "project_data_repository"),
        ("worktree", "rtl_repository"),
    ],
)
def test_fingerprint_follows_the_booley_dir_entry(tmp_path, monkeypatch, checkout, anchor):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    work_dir, anchor_root = _linked_checkout(tmp_path, checkout, anchor)
    (anchor_root / "cw").mkdir()
    (anchor_root / "cw" / "a.toml").write_bytes(b"approval\n")

    value = waiver_policy_fingerprint(work_dir)

    assert value["files"] == {"a.toml": hashlib.sha256(b"approval\n").hexdigest()}
    assert value["root"] == str((anchor_root / "cw").resolve())


@pytest.mark.parametrize(
    ("checkout", "anchor"),
    [("main", "project_data_repository"), ("worktree", "rtl_repository")],
    ids=["project_data_repository", "rtl_repository"],
)
def test_fingerprint_refuses_a_link_below_the_booley_dir_entry(
    tmp_path, monkeypatch, checkout, anchor
):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    work_dir, anchor_root = _linked_checkout(tmp_path, checkout, anchor)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(anchor_root / "cw", elsewhere, target_is_directory=True)

    with pytest.raises(ValueError, match="missing or a symlink"):
        waiver_policy_fingerprint(work_dir)
