"""Tests for Doctor's project-owned simulation fixture overlay."""

import os
import subprocess
import sys
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


@pytest.mark.skipif(sys.platform == "win32", reason="GNU Make regression requires POSIX")
def test_stage_bad_overlay_invalidates_newer_make_artifact(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "my_ip.sv"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("invalid HDL\n", encoding="utf-8")
    build_root = tmp_path / "build"
    build_root.mkdir()
    staged = build_root / "my_ip.sv"
    staged.write_text("valid HDL\n", encoding="utf-8")
    artifact = build_root / "compiled.ok"
    artifact.write_text("compiled from valid HDL\n", encoding="utf-8")
    os.utime(overlay, (100, 100))
    os.utime(staged, (200, 200))
    os.utime(artifact, (300, 300))

    assert selftest_overlay.stage_bad_overlay(project_dir, "sim", build_root) == 1
    assert staged.stat().st_mtime_ns > artifact.stat().st_mtime_ns
    result = subprocess.run(
        ["make", "-f", "-", "compiled.ok"],
        cwd=build_root,
        input="compiled.ok: my_ip.sv\n\t@echo invalid HDL was rebuilt\n\t@false\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode != 0
    assert "invalid HDL was rebuilt" in result.stdout


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_doctor_shadow_keeps_projected_core_outside_generation(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("bad\n", encoding="utf-8")
    projected = tmp_path / ".booley-projected-demo.core"
    projected.write_text("generated core\n", encoding="utf-8")
    generation_parent = project_dir / ".runtime" / "edalize" / "sim" / "slot" / "g"
    generation_parent.mkdir(parents=True)
    generation = generation_parent / "abcd"
    generation.mkdir()
    shadow = selftest_overlay.doctor_runtime_view_path(tmp_path, generation_parent, "a" * 32)

    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", tmp_path, shadow)

    assert (shadow / projected.name).is_symlink()
    assert not any(generation.rglob("*.core"))


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_doctor_shadow_rejects_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="generation parent is a symlink"):
        selftest_overlay.doctor_runtime_view_path(tmp_path, tmp_path / "linked" / "generation", "a" * 32)


def test_doctor_shadow_rejects_build_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(selftest_overlay.SelftestOverlayError, match="outside the Project"):
        selftest_overlay.doctor_runtime_view_path(project, tmp_path / "outside" / "generation", "a" * 32)


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_doctor_shadow_rejects_symlinked_generation(tmp_path: Path) -> None:
    generation = tmp_path / "generation"
    generation.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(selftest_overlay.SelftestOverlayError, match="generation parent is a symlink"):
        selftest_overlay.doctor_runtime_view_path(tmp_path, generation, "a" * 32)


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_stage_bad_run_overlay_excludes_project_dir_from_mirror(tmp_path: Path) -> None:
    """The shadow must not symlink .booley_project — it is project infra, not a
    runtime input, and FuseSoC chokes on a symlinked project dir (#586)."""
    project_dir = tmp_path / ".booley_project"
    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path
    (run_cwd / "vectors").mkdir()
    (run_cwd / "vectors" / "input.hex").write_text("vec\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", run_cwd, shadow)

    assert (shadow / "vectors").is_symlink()
    assert not (shadow / ".booley_project").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_stage_bad_run_overlay_excludes_configured_project_dir_from_mirror(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project-control" / "data"
    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(project_dir, "sim", tmp_path, shadow)

    project_relative = project_dir.relative_to(tmp_path)
    assert (shadow / project_relative.parent).is_dir()
    assert not (shadow / project_relative.parent).is_symlink()
    assert not (shadow / project_relative).exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_stage_bad_run_overlay_rejects_symlinked_project_dir_ancestor(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    project_dir = tmp_path / "linked" / "data"
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    overlay_file = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="symlinked runtime path"):
        selftest_overlay.stage_bad_run_overlay(project_dir, "sim", tmp_path, shadow)


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_managed_runtime_view_removes_view_on_normal_and_exceptional_exit(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    generation_parent = tmp_path / ".booley_project" / ".runtime" / "g"
    generation_parent.mkdir(parents=True)

    with selftest_overlay.managed_runtime_view(
        project_dir, "sim", run_cwd, generation_parent, "a" * 32
    ) as view:
        assert (view / "fixture.hex").read_text(encoding="utf-8") == "bad\n"
        assert view.is_dir()
    assert not view.exists()

    with (
        pytest.raises(ValueError, match="body failure"),
        selftest_overlay.managed_runtime_view(
            project_dir, "sim", run_cwd, generation_parent, "b" * 32
        ) as view,
    ):
        assert view.is_dir()
        raise ValueError("body failure")
    assert not view.exists()


def test_managed_runtime_view_removes_partial_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    generation_parent = tmp_path / "slot" / "g"
    generation_parent.mkdir(parents=True)
    view = generation_parent / f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-{'a' * 32}"

    def fail_after_partial(*args: object, **kwargs: object) -> int:
        view.mkdir()
        (view / "partial").write_text("partial\n", encoding="utf-8")
        raise OSError("staging failed")

    monkeypatch.setattr(selftest_overlay, "stage_bad_run_overlay", fail_after_partial)
    with (
        pytest.raises(selftest_overlay.SelftestOverlayError, match="staging failed"),
        selftest_overlay.managed_runtime_view(
            project_dir,
            "sim",
            tmp_path,
            generation_parent,
            "a" * 32,
        ),
    ):
        raise AssertionError("staging should fail")
    assert not view.exists()


def test_managed_runtime_view_retries_cleanup_and_preserves_body_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    generation_parent = tmp_path / "slot" / "g"
    generation_parent.mkdir(parents=True)
    view = generation_parent / f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-{'a' * 32}"
    remove = selftest_overlay._remove_shadow
    failed = False

    def fail_once(path: Path) -> None:
        nonlocal failed
        if path == view and not failed:
            failed = True
            raise OSError("cleanup failed")
        remove(path)

    original_stage = selftest_overlay.stage_bad_run_overlay

    def stage_then_fail(*args: object, **kwargs: object) -> int:
        result = original_stage(*args, **kwargs)
        monkeypatch.setattr(selftest_overlay, "_remove_shadow", fail_once)
        return result

    monkeypatch.setattr(selftest_overlay, "stage_bad_run_overlay", stage_then_fail)
    with (
        pytest.raises(ValueError, match="body failure") as error,
        selftest_overlay.managed_runtime_view(
            project_dir, "sim", run_cwd, generation_parent, "a" * 32
        ),
    ):
        raise ValueError("body failure")
    assert any("cleanup failed" in note for note in error.value.__notes__)
    assert view.is_dir()
    monkeypatch.setattr(selftest_overlay, "_remove_shadow", remove)
    monkeypatch.setattr(selftest_overlay, "stage_bad_run_overlay", original_stage)

    with selftest_overlay.managed_runtime_view(
        project_dir, "sim", run_cwd, generation_parent, "b" * 32
    ):
        pass
    assert not view.exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_managed_runtime_view_prunes_only_owned_siblings(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    overlay = selftest_overlay.bad_overlay_dir(project_dir, "sim") / "fixture.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("bad\n", encoding="utf-8")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    generation_parent = tmp_path / "slot" / "g"
    generation_parent.mkdir(parents=True)
    for name in (
        f"{selftest_overlay.BAD_RUN_CWD_DIR}-0123456789abcdef",
        f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-{'a' * 32}",
    ):
        (generation_parent / name).mkdir()
    unrelated = generation_parent / f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-not-a-token"
    unrelated.mkdir()
    other_slot = tmp_path / "other" / "g"
    other_slot.mkdir(parents=True)
    (other_slot / f"{selftest_overlay.BAD_RUN_CWD_DIR}-0123456789abcdef").mkdir()

    with selftest_overlay.managed_runtime_view(
        project_dir, "sim", run_cwd, generation_parent, "b" * 32
    ):
        assert unrelated.is_dir()
        assert (other_slot / f"{selftest_overlay.BAD_RUN_CWD_DIR}-0123456789abcdef").is_dir()
    assert not any(
        child.name.endswith("0123456789abcdef") or child.name.endswith(f"a-{'a' * 32}")
        for child in generation_parent.iterdir()
    )
    assert unrelated.is_dir()


def test_doctor_runtime_view_rejects_invalid_attempt_token(tmp_path: Path) -> None:
    with pytest.raises(selftest_overlay.SelftestOverlayError, match="invalid Doctor attempt token"):
        selftest_overlay.doctor_runtime_view_path(tmp_path, tmp_path / "slot", "not-a-token")
