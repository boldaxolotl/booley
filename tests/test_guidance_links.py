"""Tests for guidance_links: root AGENTS.md/CLAUDE.md links to the canonical file."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.guidance_links import (
    CANON_NAME,
    LINK_NAMES,
    ensure_guidance_links,
)


def _make_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (project_root, project_dir, canon) with a git repo and canon file."""
    project_root = tmp_path / "repo"
    project_dir = project_root / ".booley_project"
    project_dir.mkdir(parents=True)
    (project_root / ".git" / "info").mkdir(parents=True)
    canon = project_dir / CANON_NAME
    canon.write_text("# AGENTS\n", encoding="utf-8")
    return project_root, project_dir, canon


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink assertions assume POSIX symlinks",
)


def test_creates_both_root_links(tmp_path: Path):
    project_root, project_dir, canon = _make_project(tmp_path)

    links = ensure_guidance_links(project_root, project_dir)

    assert {p.name for p in links} == set(LINK_NAMES)
    for name in LINK_NAMES:
        link = project_root / name
        assert link.is_symlink()
        assert link.resolve() == canon.resolve()


def test_relative_symlink_target(tmp_path: Path):
    project_root, project_dir, _ = _make_project(tmp_path)

    ensure_guidance_links(project_root, project_dir)

    # Link points at the canon via a relative path (survives repo relocation).
    target = (project_root / "AGENTS.md").readlink()
    assert not target.is_absolute()
    assert target == Path(".booley_project") / CANON_NAME


def test_idempotent(tmp_path: Path):
    project_root, project_dir, _ = _make_project(tmp_path)

    first = ensure_guidance_links(project_root, project_dir)
    before = {p: p.readlink() for p in first}
    second = ensure_guidance_links(project_root, project_dir)
    after = {p: p.readlink() for p in second}

    assert before == after


def test_replaces_legacy_real_file(tmp_path: Path):
    project_root, project_dir, canon = _make_project(tmp_path)
    # A pre-existing real AGENTS.md at the root (the old layout) is replaced.
    legacy = project_root / "AGENTS.md"
    legacy.write_text("stale root copy\n", encoding="utf-8")

    ensure_guidance_links(project_root, project_dir)

    assert legacy.is_symlink()
    assert legacy.resolve() == canon.resolve()


def test_preserves_matching_tracked_root_file(tmp_path: Path):
    project_root, project_dir, canon = _make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    root_agents = project_root / "AGENTS.md"
    root_agents.write_bytes(canon.read_bytes())
    subprocess.run(["git", "add", "AGENTS.md"], cwd=project_root, check=True)

    ensure_guidance_links(project_root, project_dir)

    assert root_agents.is_file()
    assert not root_agents.is_symlink()
    assert root_agents.read_bytes() == canon.read_bytes()
    assert (project_root / "CLAUDE.md").is_symlink()


def test_refuses_to_overwrite_stale_tracked_root_file(tmp_path: Path):
    project_root, project_dir, _canon = _make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    root_agents = project_root / "AGENTS.md"
    root_agents.write_text("# TRACKED PROJECT GUIDANCE\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=project_root, check=True)

    with pytest.raises(OSError, match="tracked root guidance file differs"):
        ensure_guidance_links(project_root, project_dir)

    assert root_agents.read_text(encoding="utf-8") == "# TRACKED PROJECT GUIDANCE\n"
    assert not root_agents.is_symlink()


def test_updates_git_info_exclude(tmp_path: Path):
    project_root, project_dir, _ = _make_project(tmp_path)

    ensure_guidance_links(project_root, project_dir)

    exclude = (project_root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/AGENTS.md" in exclude.splitlines()
    assert "/CLAUDE.md" in exclude.splitlines()

    # Re-running does not duplicate entries.
    ensure_guidance_links(project_root, project_dir)
    lines = (project_root / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count("/AGENTS.md") == 1
    assert lines.count("/CLAUDE.md") == 1


def test_missing_canon_raises(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_dir = project_root / ".booley_project"
    project_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        ensure_guidance_links(project_root, project_dir)


def test_preserves_existing_exclude_entries(tmp_path: Path):
    project_root, project_dir, _ = _make_project(tmp_path)
    exclude_path = project_root / ".git" / "info" / "exclude"
    exclude_path.write_text("# pre-existing\n/build\n", encoding="utf-8")

    ensure_guidance_links(project_root, project_dir)

    lines = exclude_path.read_text(encoding="utf-8").splitlines()
    assert "/build" in lines
    assert "/AGENTS.md" in lines
