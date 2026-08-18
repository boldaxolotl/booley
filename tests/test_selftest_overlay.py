"""Tests for Doctor's project-owned simulation fixture overlay."""

from pathlib import Path

import pytest

from booley.fusesoc import selftest_overlay


def test_stage_bad_overlay_replaces_only_mirrored_build_files(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim")
    overlay_file = overlay / "firmware" / "firmware.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    build_root = tmp_path / "build"
    staged = build_root / "firmware" / "firmware.hex"
    staged.parent.mkdir(parents=True)
    staged.write_text("good\n", encoding="utf-8")

    assert selftest_overlay.has_bad_overlay(project_dir, "sim")
    assert selftest_overlay.stage_bad_overlay(project_dir, "sim", build_root) == 1
    assert staged.read_text(encoding="utf-8") == "bad\n"


def test_stage_bad_overlay_rejects_symlinks(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim")
    overlay.mkdir(parents=True)
    (overlay / "redirect").symlink_to(tmp_path)

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="symlink"):
        selftest_overlay.stage_bad_overlay(project_dir, "sim", tmp_path / "build")
