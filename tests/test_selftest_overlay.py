"""Tests for Doctor's project-owned simulation fixture overlay."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.fusesoc import selftest_overlay
from tests.time_budget import hang_guard_s

_SYMLINKS = pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)


def _write_overlay(booley_dir: Path, relative: str = "fixture.hex") -> Path:
    """Write the bad simulation fixture ``relative`` under ``booley_dir``'s overlay."""
    path = selftest_overlay.bad_overlay_dir(booley_dir, "sim") / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("bad\n", encoding="utf-8")
    return path


@pytest.fixture
def bad_booley_dir(tmp_path: Path) -> Path:
    """A Booley Directory whose simulation overlay holds only ``fixture.hex``."""
    booley_dir = tmp_path / ".booley"
    _write_overlay(booley_dir)
    return booley_dir


@pytest.fixture
def run_cwd(tmp_path: Path) -> Path:
    """An empty runtime-assets directory."""
    path = tmp_path / "runtime-assets"
    path.mkdir()
    return path


@pytest.fixture
def generation_parent(tmp_path: Path) -> Path:
    """An empty generation parent inside the project."""
    path = tmp_path / "slot" / "g"
    path.mkdir(parents=True)
    return path


def test_stage_bad_overlay_replaces_only_mirrored_build_files(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    overlay = selftest_overlay.bad_overlay_dir(booley_dir, "sim")
    overlay_file = overlay / "firmware" / "firmware.hex"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("bad\n", encoding="utf-8")
    build_root = tmp_path / "build"
    staged = build_root / "firmware" / "firmware.hex"
    staged.parent.mkdir(parents=True)
    staged.write_text("good\n", encoding="utf-8")

    assert selftest_overlay.has_bad_overlay(booley_dir, "sim")
    assert selftest_overlay.stage_bad_overlay(booley_dir, "sim", build_root) == 1
    assert staged.read_text(encoding="utf-8") == "bad\n"


@pytest.mark.skipif(sys.platform == "win32", reason="GNU Make regression requires POSIX")
def test_stage_bad_overlay_invalidates_newer_make_artifact(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    overlay = selftest_overlay.bad_overlay_dir(booley_dir, "sim") / "my_ip.sv"
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

    assert selftest_overlay.stage_bad_overlay(booley_dir, "sim", build_root) == 1
    assert staged.stat().st_mtime_ns > artifact.stat().st_mtime_ns
    # Kept on subprocess.run: needs stdin data (makefile on `-f -`), which the bounded helper does not take.
    result = subprocess.run(
        ["make", "-f", "-", "compiled.ok"],
        cwd=build_root,
        input="compiled.ok: my_ip.sv\n\t@echo invalid HDL was rebuilt\n\t@false\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=hang_guard_s(),
    )

    assert result.returncode != 0
    assert "invalid HDL was rebuilt" in result.stdout


def test_stage_bad_overlay_rejects_symlinks(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    overlay = selftest_overlay.bad_overlay_dir(booley_dir, "sim")
    overlay.mkdir(parents=True)
    (overlay / "redirect").symlink_to(tmp_path)

    with pytest.raises(selftest_overlay.SelftestOverlayError, match="symlink"):
        selftest_overlay.stage_bad_overlay(booley_dir, "sim", tmp_path / "build")


def test_stage_bad_run_overlay_shadows_runtime_assets_without_mutating_them(
    tmp_path: Path, run_cwd: Path
) -> None:
    booley_dir = tmp_path / ".booley"
    _write_overlay(booley_dir, "firmware/firmware.hex")
    runtime_file = run_cwd / "firmware" / "firmware.hex"
    runtime_file.parent.mkdir()
    runtime_file.write_text("good\n", encoding="utf-8")
    sibling = run_cwd / "vectors" / "input.hex"
    sibling.parent.mkdir()
    sibling.write_text("vector\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    assert selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", run_cwd, shadow) == 1

    assert (shadow / "firmware" / "firmware.hex").read_text(encoding="utf-8") == "bad\n"
    assert (shadow / "vectors" / "input.hex").read_text(encoding="utf-8") == "vector\n"
    assert (shadow / "vectors").is_symlink()
    assert runtime_file.read_text(encoding="utf-8") == "good\n"


@_SYMLINKS
def test_doctor_shadow_keeps_projected_core_outside_generation(
    tmp_path: Path, bad_booley_dir: Path
) -> None:
    projected = tmp_path / ".booley-projected-demo.core"
    projected.write_text("generated core\n", encoding="utf-8")
    generation_parent = bad_booley_dir / ".runtime" / "edalize" / "sim" / "slot" / "g"
    generation_parent.mkdir(parents=True)
    generation = generation_parent / "abcd"
    generation.mkdir()
    shadow = selftest_overlay.doctor_runtime_view_path(tmp_path, generation_parent, "a" * 32)

    selftest_overlay.stage_bad_run_overlay(bad_booley_dir, "sim", tmp_path, shadow)

    assert (shadow / projected.name).is_symlink()
    assert not any(generation.rglob("*.core"))


def _symlinked_parent(tmp_path: Path) -> tuple[Path, Path, str, str]:
    (tmp_path / "outside").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    return tmp_path, tmp_path / "linked" / "generation", "a" * 32, "generation parent is a symlink"


def _outside_project(tmp_path: Path) -> tuple[Path, Path, str, str]:
    (tmp_path / "project").mkdir()
    return (
        tmp_path / "project",
        tmp_path / "outside" / "generation",
        "a" * 32,
        "outside the Project",
    )


def _symlinked_generation(tmp_path: Path) -> tuple[Path, Path, str, str]:
    generation = tmp_path / "generation"
    generation.symlink_to(tmp_path / "outside", target_is_directory=True)
    return tmp_path, generation, "a" * 32, "generation parent is a symlink"


def _invalid_token(tmp_path: Path) -> tuple[Path, Path, str, str]:
    return tmp_path, tmp_path / "slot", "not-a-token", "invalid Doctor attempt token"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(_symlinked_parent, marks=_SYMLINKS, id="symlinked-parent"),
        pytest.param(_outside_project, id="build-outside-project"),
        pytest.param(_symlinked_generation, marks=_SYMLINKS, id="symlinked-generation"),
        pytest.param(_invalid_token, id="invalid-attempt-token"),
    ],
)
def test_doctor_runtime_view_path_rejects(
    tmp_path: Path, case: Callable[[Path], tuple[Path, Path, str, str]]
) -> None:
    project, generation, token, match = case(tmp_path)

    with pytest.raises(selftest_overlay.SelftestOverlayError, match=match):
        selftest_overlay.doctor_runtime_view_path(project, generation, token)


@_SYMLINKS
def test_stage_bad_run_overlay_excludes_booley_dir_from_mirror(
    tmp_path: Path, bad_booley_dir: Path
) -> None:
    """The shadow must not symlink .booley — it is project infra, not a
    runtime input, and FuseSoC chokes on a symlinked Booley Directory (#586)."""
    (tmp_path / "vectors").mkdir()
    (tmp_path / "vectors" / "input.hex").write_text("vec\n", encoding="utf-8")
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(bad_booley_dir, "sim", tmp_path, shadow)

    assert (shadow / "vectors").is_symlink()
    assert not (shadow / ".booley").exists()


@_SYMLINKS
def test_stage_bad_run_overlay_excludes_configured_booley_dir_from_mirror(
    tmp_path: Path,
) -> None:
    booley_dir = tmp_path / "project-control" / "data"
    _write_overlay(booley_dir)
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", tmp_path, shadow)

    project_relative = booley_dir.relative_to(tmp_path)
    assert (shadow / project_relative.parent).is_dir()
    assert not (shadow / project_relative.parent).is_symlink()
    assert not (shadow / project_relative).exists()


def _symlinked_booley_dir_ancestor(tmp_path: Path) -> tuple[Path, Path, str]:
    (tmp_path / "outside").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    booley_dir = tmp_path / "linked" / "data"
    _write_overlay(booley_dir)
    return booley_dir, tmp_path, "symlinked runtime path"


def _symlinked_overlay_ancestor(tmp_path: Path) -> tuple[Path, Path, str]:
    booley_dir = tmp_path / ".booley"
    _write_overlay(booley_dir, "firmware/firmware.hex")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    (run_cwd / "firmware").symlink_to(tmp_path / "outside")
    return booley_dir, run_cwd, "symlinked runtime path"


def _file_as_overlay_ancestor(tmp_path: Path) -> tuple[Path, Path, str]:
    booley_dir = tmp_path / ".booley"
    _write_overlay(booley_dir, "firmware/firmware.hex")
    run_cwd = tmp_path / "runtime-assets"
    run_cwd.mkdir()
    (run_cwd / "firmware").write_text("not a directory\n", encoding="utf-8")
    return booley_dir, run_cwd, "not a directory"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(_symlinked_booley_dir_ancestor, marks=_SYMLINKS, id="booley-dir-ancestor"),
        pytest.param(_symlinked_overlay_ancestor, id="symlinked-overlay-ancestor"),
        pytest.param(_file_as_overlay_ancestor, id="file-as-overlay-ancestor"),
    ],
)
def test_stage_bad_run_overlay_rejects(
    tmp_path: Path, case: Callable[[Path], tuple[Path, Path, str]]
) -> None:
    booley_dir, run_cwd, match = case(tmp_path)
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR

    with pytest.raises(selftest_overlay.SelftestOverlayError, match=match):
        selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", run_cwd, shadow)


def test_stage_bad_run_overlay_replaces_stale_shadow_forms(
    tmp_path: Path, bad_booley_dir: Path, run_cwd: Path
) -> None:
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR
    shadow.parent.mkdir()
    shadow.write_text("stale file\n", encoding="utf-8")

    selftest_overlay.stage_bad_run_overlay(bad_booley_dir, "sim", run_cwd, shadow)
    (shadow / "stale").write_text("stale directory\n", encoding="utf-8")
    selftest_overlay.stage_bad_run_overlay(bad_booley_dir, "sim", run_cwd, shadow)

    assert not (shadow / "stale").exists()
    assert (shadow / "fixture.hex").read_text(encoding="utf-8") == "bad\n"


def test_stage_bad_run_overlay_materializes_missing_runtime_branch(
    tmp_path: Path, run_cwd: Path
) -> None:
    booley_dir = tmp_path / ".booley"
    _write_overlay(booley_dir, "new/fixture.hex")
    shadow = run_cwd / selftest_overlay.BAD_RUN_CWD_DIR

    selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", run_cwd, shadow)

    assert (shadow / "new" / "fixture.hex").read_text(encoding="utf-8") == "bad\n"


def test_stage_bad_run_overlay_handles_absent_fixture_and_runtime(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    shadow = tmp_path / "build" / selftest_overlay.BAD_RUN_CWD_DIR
    missing = tmp_path / "missing-runtime"

    assert selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", missing, shadow) == 0

    _write_overlay(booley_dir)
    with pytest.raises(selftest_overlay.SelftestOverlayError, match="not a directory"):
        selftest_overlay.stage_bad_run_overlay(booley_dir, "sim", missing, shadow)


@_SYMLINKS
def test_managed_runtime_view_removes_view_on_normal_and_exceptional_exit(
    tmp_path: Path, bad_booley_dir: Path, run_cwd: Path, generation_parent: Path
) -> None:
    with selftest_overlay.managed_runtime_view(
        tmp_path, bad_booley_dir, "sim", run_cwd, generation_parent, "a" * 32
    ) as view:
        assert (view / "fixture.hex").read_text(encoding="utf-8") == "bad\n"
        assert view.is_dir()
    assert not view.exists()

    with (
        pytest.raises(ValueError, match="body failure"),
        selftest_overlay.managed_runtime_view(
            tmp_path, bad_booley_dir, "sim", run_cwd, generation_parent, "b" * 32
        ) as view,
    ):
        assert view.is_dir()
        raise ValueError("body failure")
    assert not view.exists()


def test_managed_runtime_view_removes_partial_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generation_parent: Path
) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    view = generation_parent / f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-{'a' * 32}"

    def fail_after_partial(*args: object, **kwargs: object) -> int:
        view.mkdir()
        (view / "partial").write_text("partial\n", encoding="utf-8")
        raise OSError("staging failed")

    monkeypatch.setattr(selftest_overlay, "stage_bad_run_overlay", fail_after_partial)
    with (
        pytest.raises(selftest_overlay.SelftestOverlayError, match="staging failed"),
        selftest_overlay.managed_runtime_view(
            tmp_path, booley_dir, "sim", tmp_path, generation_parent, "a" * 32
        ),
    ):
        raise AssertionError("staging should fail")
    assert not view.exists()


def test_managed_runtime_view_retries_cleanup_and_preserves_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_booley_dir: Path,
    run_cwd: Path,
    generation_parent: Path,
) -> None:
    view = generation_parent / f"{selftest_overlay.BAD_RUN_CWD_DIR}-a-{'a' * 32}"
    real_rmtree = shutil.rmtree
    failed = False

    def rmtree_failing_once_for_view(path: Path, *args: object, **kwargs: object) -> None:
        """Filesystem stand-in: the first removal of the view fails, as on a busy disk."""
        nonlocal failed
        if path == view and not failed:
            failed = True
            raise OSError("cleanup failed")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", rmtree_failing_once_for_view)
    with (
        pytest.raises(ValueError, match="body failure") as error,
        selftest_overlay.managed_runtime_view(
            tmp_path, bad_booley_dir, "sim", run_cwd, generation_parent, "a" * 32
        ),
    ):
        raise ValueError("body failure")
    assert any("cleanup failed" in note for note in error.value.__notes__)
    assert view.is_dir()

    with selftest_overlay.managed_runtime_view(
        tmp_path, bad_booley_dir, "sim", run_cwd, generation_parent, "b" * 32
    ):
        pass
    assert not view.exists()


@_SYMLINKS
def test_managed_runtime_view_prunes_only_owned_siblings(
    tmp_path: Path, bad_booley_dir: Path, run_cwd: Path, generation_parent: Path
) -> None:
    prefix = selftest_overlay.BAD_RUN_CWD_DIR
    for name in (f"{prefix}-0123456789abcdef", f"{prefix}-a-{'a' * 32}"):
        (generation_parent / name).mkdir()
    unrelated = generation_parent / f"{prefix}-a-not-a-token"
    unrelated.mkdir()
    other_view = tmp_path / "other" / "g" / f"{prefix}-0123456789abcdef"
    other_view.mkdir(parents=True)

    with selftest_overlay.managed_runtime_view(
        tmp_path, bad_booley_dir, "sim", run_cwd, generation_parent, "b" * 32
    ):
        assert unrelated.is_dir()
        assert other_view.is_dir()
    assert not any(
        child.name.endswith("0123456789abcdef") or child.name.endswith(f"a-{'a' * 32}")
        for child in generation_parent.iterdir()
    )
    assert unrelated.is_dir()


@_SYMLINKS
def test_managed_runtime_view_validates_slot_against_checkout_root(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    booley_dir = tmp_path / "external-state" / ".booley"
    _write_overlay(booley_dir)
    run_cwd = checkout / "runtime-assets"
    run_cwd.mkdir(parents=True)
    generation_parent = checkout / ".booley" / ".runtime" / "g"
    generation_parent.mkdir(parents=True)

    with selftest_overlay.managed_runtime_view(
        checkout, booley_dir, "sim", run_cwd, generation_parent, "c" * 32
    ) as view:
        assert (view / "fixture.hex").read_text(encoding="utf-8") == "bad\n"
    assert not view.exists()


@_SYMLINKS
def test_managed_runtime_view_follows_only_a_symlinked_booley_dir_entry(tmp_path: Path) -> None:
    """A Codespace links ``/work/.booley``; links below the entry stay refused."""
    checkout = tmp_path / "work"
    data = tmp_path / "workspaces" / "repo"
    elsewhere = tmp_path / "elsewhere"
    for directory in (checkout, data, elsewhere):
        directory.mkdir(parents=True)
    (checkout / ".booley").symlink_to(data, target_is_directory=True)
    booley_dir = checkout / ".booley"
    _write_overlay(booley_dir)
    run_cwd = checkout / "runtime-assets"
    run_cwd.mkdir()
    generation_parent = booley_dir / ".runtime" / "g"
    generation_parent.mkdir(parents=True)

    with selftest_overlay.managed_runtime_view(
        checkout, booley_dir, "sim", run_cwd, generation_parent, "d" * 32
    ) as view:
        assert (view / "fixture.hex").read_text(encoding="utf-8") == "bad\n"
    assert not view.exists()

    (data / "linked").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(
        selftest_overlay.SelftestOverlayError, match="generation parent is a symlink"
    ):
        selftest_overlay.doctor_runtime_view_path(checkout, booley_dir / "linked" / "g", "d" * 32)


def test_sim_doctor_overlay_reads_the_checkouts_selftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bad fixture comes from the checkout's ``selftest/``, not from ``$BOOLEY_DIR``."""
    from booley.flows.sim import build
    from booley.runtime import booley_dir

    checkout = tmp_path / "checkout"
    _write_overlay(checkout / ".booley", "bad.hex")
    other = tmp_path / "other-booley"
    other.mkdir()
    build_root = tmp_path / "build"
    build_root.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(other))
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    booley_dir.reset_cache()
    try:
        # private: the sim build stages the overlay inside a larger build; no lighter public entry.
        build._stage_doctor_overlay(checkout, build_root)
    finally:
        booley_dir.reset_cache()

    assert (build_root / "bad.hex").read_text(encoding="utf-8") == "bad\n"


def test_prepared_group_runtime_view_reads_the_checkouts_selftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Doctor view of a prepared group mirrors the checkout's ``selftest/``, not ``$BOOLEY_DIR``'s."""
    from types import SimpleNamespace

    from booley.flows.sim.execution.engine import PreparedOrdinaryGroup
    from booley.runtime import booley_dir

    checkout = tmp_path / "checkout"
    _write_overlay(checkout / ".booley", "bad.hex")
    other = tmp_path / "other-booley"
    other.mkdir()
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    work_root = checkout / ".booley" / ".runtime" / "g" / "work"
    work_root.mkdir(parents=True)
    # Only the two attributes ``runtime_view`` reads: the Target's root and the work root.
    # private: PreparedOrdinaryGroup internals (_handle, _attempt); building a real group needs a full sim build.
    group = object.__new__(PreparedOrdinaryGroup)
    group._handle = SimpleNamespace(project_root=checkout)  # type: ignore[assignment]
    group._attempt = SimpleNamespace(  # type: ignore[assignment]
        prepared=SimpleNamespace(work_root=work_root)
    )
    monkeypatch.setenv("BOOLEY_DIR", str(other))
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    booley_dir.reset_cache()
    try:
        with group.runtime_view(run_cwd, "e" * 32) as view:
            assert (view / "bad.hex").read_text(encoding="utf-8") == "bad\n"
    finally:
        booley_dir.reset_cache()
