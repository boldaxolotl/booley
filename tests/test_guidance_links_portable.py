"""Cross-platform guidance-link behavior: the two-venue link target (F-13).

The project dir is bind-mounted at ``/booley-project`` inside the Session
Runtime — outside the workspace. A link relative to *that* reads
``../booley-project/AGENTS.md``, which on the host resolves to a sibling of the
repo that does not exist, so a host-side editor or agent reads no guidance.

Unlike ``test_guidance_links.py`` (POSIX-only: it asserts on symlinks), these
tests run everywhere — the Windows hardlink/copy fallback is exactly where the
host-side repair has to work.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.guidance_links import (
    CANON_NAME,
    LINK_NAMES,
    _points_to,
    _portable_target,
    ensure_guidance_links,
)
from booley.project_dir import PROJECT_DIR_NAME


def _make_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "repo"
    project_dir = project_root / PROJECT_DIR_NAME
    project_dir.mkdir(parents=True)
    (project_root / ".git" / "info").mkdir(parents=True)
    canon = project_dir / CANON_NAME
    canon.write_text("# AGENTS\n", encoding="utf-8")
    return project_root, project_dir, canon


class TestPortableTarget:
    def test_prefers_the_repo_local_path(self, tmp_path: Path):
        project_root, _project_dir, canon = _make_project(tmp_path)
        target = _portable_target(project_root, canon.resolve())
        assert target == project_root / PROJECT_DIR_NAME / CANON_NAME

    def test_container_layout_still_resolves_through_the_workspace_mount(self, tmp_path: Path):
        """In-container the canon is /booley-project/AGENTS.md, but the same file
        is reachable at <repo>/.booley_project/AGENTS.md. Name the latter."""
        project_root, _project_dir, _canon = _make_project(tmp_path)
        mounted_canon = tmp_path / "booley-project" / CANON_NAME
        mounted_canon.parent.mkdir()
        mounted_canon.write_text("# AGENTS\n", encoding="utf-8")

        target = _portable_target(project_root, mounted_canon)

        assert target == project_root / PROJECT_DIR_NAME / CANON_NAME
        assert ".." not in str(target)

    def test_relocated_project_dir_falls_back_to_canon(self, tmp_path: Path):
        """A [project].dir override has no repo-local path to name."""
        project_root = tmp_path / "repo"
        (project_root / ".git" / "info").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        canon = elsewhere / CANON_NAME
        canon.write_text("# AGENTS\n", encoding="utf-8")

        assert _portable_target(project_root, canon) == canon


class TestEnsureGuidanceLinks:
    def test_creates_both_links_readable_from_the_host(self, tmp_path: Path):
        project_root, project_dir, _canon = _make_project(tmp_path)

        links = ensure_guidance_links(project_root, project_dir)

        assert {p.name for p in links} == set(LINK_NAMES)
        for link in links:
            # The point of F-13: readable, whatever link flavor the OS allowed.
            assert link.read_text(encoding="utf-8") == "# AGENTS\n"

    def test_is_idempotent(self, tmp_path: Path):
        project_root, project_dir, _canon = _make_project(tmp_path)

        first = ensure_guidance_links(project_root, project_dir)
        stats = {p: p.stat().st_ino for p in first}
        ensure_guidance_links(project_root, project_dir)

        # A hardlink resolves to itself; without the samefile check in
        # _points_to, every run would tear it down and rebuild it.
        for p, ino in stats.items():
            if ino:  # st_ino is 0 on some filesystems
                assert p.stat().st_ino == ino

    def test_repairs_a_dangling_container_style_link(self, tmp_path: Path):
        """The exact F-13 breakage: a link left pointing at ../booley-project/."""
        project_root, project_dir, _canon = _make_project(tmp_path)
        link = project_root / "AGENTS.md"
        try:
            link.symlink_to(Path("..") / "booley-project" / CANON_NAME)
        except OSError:
            pytest.skip("symlink creation not permitted on this host")

        assert not link.exists()  # dangling, as seen from the host
        ensure_guidance_links(project_root, project_dir)
        assert link.read_text(encoding="utf-8") == "# AGENTS\n"

    def test_replaces_a_stale_copy(self, tmp_path: Path):
        """A copy silently goes stale; it must not be mistaken for a live link."""
        project_root, project_dir, canon = _make_project(tmp_path)
        link = project_root / "AGENTS.md"
        link.write_text("# STALE\n", encoding="utf-8")

        assert not _points_to(link, canon.resolve())
        ensure_guidance_links(project_root, project_dir)
        assert link.read_text(encoding="utf-8") == "# AGENTS\n"

    def test_missing_canon_raises(self, tmp_path: Path):
        project_root = tmp_path / "repo"
        project_dir = project_root / PROJECT_DIR_NAME
        project_dir.mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            ensure_guidance_links(project_root, project_dir)


class TestPointsTo:
    def test_hardlink_is_recognized_by_identity(self, tmp_path: Path):
        canon = tmp_path / CANON_NAME
        canon.write_text("x\n", encoding="utf-8")
        link = tmp_path / "CLAUDE.md"
        try:
            os.link(canon, link)
        except OSError:
            pytest.skip("hardlinks not supported here")
        assert _points_to(link, canon.resolve()) is True

    def test_independent_copy_is_not(self, tmp_path: Path):
        canon = tmp_path / CANON_NAME
        canon.write_text("x\n", encoding="utf-8")
        copy = tmp_path / "CLAUDE.md"
        copy.write_text("x\n", encoding="utf-8")
        assert _points_to(copy, canon.resolve()) is False

    def test_absent_link_is_not(self, tmp_path: Path):
        canon = tmp_path / CANON_NAME
        canon.write_text("x\n", encoding="utf-8")
        assert _points_to(tmp_path / "nope.md", canon.resolve()) is False
