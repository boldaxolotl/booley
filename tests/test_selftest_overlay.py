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


def test_stage_bad_run_overlay_shadows_runtime_assets_without_mutating_them(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay_file = (
        selftest_overlay.bad_overlay_dir(project_dir, "sim") / "firmware" / "firmware.hex"
    )
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    runtime_file = run_cwd / "firmware" / "firmware.hex"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("good\n", encoding="utf-8")
    sibling = run_cwd / "vectors" / "input.hex"
    sibling.parent.mkdir()
    sibling.write_text("vector\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    assert selftest_overlay.stage_bad_run_overlay(project_dir, "sim", run_cwd, shadow) == 1

    assert (shadow / "firmware" / "firmware.hex").read_text(encoding="utf-8") == "bad\n"
    assert (shadow / "vectors" / "input.hex").read_text(encoding="utf-8") == "vector\n"
    assert (shadow / "vectors").is_symlink()
    assert runtime_file.read_text(encoding="utf-8") == "good\n"


def test_stage_bad_run_overlay_rejects_symlinked_overlay_ancestor(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay_file = (
        selftest_overlay.bad_overlay_dir(project_dir, "sim") / "firmware" / "firmware.hex"
    )
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    (run_cwd / "firmware").symlink_to(tmp_path / "outside")

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="symlinked runtime path"):
        selftest_overlay.stage_bad_run_overlay(
            project_dir,
            "sim",
            run_cwd,
            tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR,
        )


def test_stage_bad_run_overlay_replaces_stale_shadow_forms(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR
    shadow.parent.mkdir()
    shadow.write_text("stale file\n", encoding="utf-8")

    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", run_cwd, shadow)
    (shadow / "stale").write_text("stale directory\n", encoding="utf-8")
    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", run_cwd, shadow)

    assert not (shadow / "stale").exists()
    assert (shadow / "fixture.hex").read_text(encoding="utf-8") == "bad\n"


def test_stage_bad_run_overlay_materializes_missing_runtime_branch(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "new" / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    shadow = run_cwd / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", run_cwd, shadow)

    assert (shadow / "new" / "fixture.hex").read_text(encoding="utf-8") == "bad\n"


def test_stage_bad_run_overlay_rejects_file_as_overlay_ancestor(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay_file = (
        selftest_overlay.bad_overlay_dir(project_dir, "sim") / "firmware" / "firmware.hex"
    )
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    (run_cwd / "firmware").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="not a directory"):
        selftest_overlay.stage_bad_run_overlay(
            project_dir,
            "sim",
            run_cwd,
            tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR,
        )


def test_stage_bad_run_overlay_handles_absent_fixture_and_runtime(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    assert (
        selftest_overlay.stage_bad_run_overlay(
            project_dir, "sim", tmp_path / "missing-runtime", shadow
        )
        == 0
    )

    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    with pytest.raises(selftest_overlay.SelftestOverlayError, match="not a directory"):
        selftest_overlay.stage_bad_run_overlay(
            project_dir, "sim", tmp_path / "missing-runtime", shadow
        )
