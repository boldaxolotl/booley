"""Runtime-input materialization at the Simulation adapter seam."""

from pathlib import Path

import pytest

from booley.flows.sim.runtime_inputs import RuntimeInputError, materialize_runtime_inputs


def test_materialized_input_exists_only_during_the_run(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    source = build / "vectors" / "input.hex"
    source.parent.mkdir(parents=True)
    source.write_text("01\n", encoding="utf-8")
    run.mkdir()

    with materialize_runtime_inputs(build, run, ("vectors/input.hex",)):
        staged = run / "vectors" / "input.hex"
        assert staged.is_symlink()
        assert staged.read_text(encoding="utf-8") == "01\n"

    assert not (run / "vectors").exists()
    assert source.read_text(encoding="utf-8") == "01\n"


def test_nested_input_does_not_expose_undeclared_build_files(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    vectors = build / "vectors"
    vectors.mkdir(parents=True)
    (vectors / "declared.hex").write_text("01\n", encoding="utf-8")
    (vectors / "private.hex").write_text("ff\n", encoding="utf-8")
    stale_overlay = build / ".booley-runtime-inputs" / "vectors"
    stale_overlay.mkdir(parents=True)
    (stale_overlay / "stale.hex").write_text("stale\n", encoding="utf-8")
    run.mkdir()

    with materialize_runtime_inputs(build, run, ("vectors/declared.hex",)):
        assert (run / "vectors" / "declared.hex").read_text(encoding="utf-8") == "01\n"
        assert not (run / "vectors" / "private.hex").exists()
        assert not (run / "vectors" / "stale.hex").exists()

    assert not (run / "vectors").exists()


def test_matching_project_input_is_preserved(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    (build / "firmware.hex").write_text("01\n", encoding="utf-8")
    existing = run / "firmware.hex"
    existing.write_text("01\n", encoding="utf-8")

    with materialize_runtime_inputs(build, run, ("firmware.hex",)):
        assert existing.is_symlink() is False

    assert existing.read_text(encoding="utf-8") == "01\n"


def test_stale_materialized_symlink_is_adopted_and_cleaned(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    source = build / "firmware.hex"
    source.write_text("01\n", encoding="utf-8")
    overlay = build / ".booley-runtime-inputs"
    overlay.mkdir()
    staged_source = overlay / "firmware.hex"
    staged_source.symlink_to(source)
    stale = run / "firmware.hex"
    stale.symlink_to(staged_source)

    with materialize_runtime_inputs(build, run, ("firmware.hex",)):
        assert stale.read_text(encoding="utf-8") == "01\n"

    assert not stale.exists()


def test_matching_project_symlink_is_preserved(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    source = build / "firmware.hex"
    source.write_text("01\n", encoding="utf-8")
    existing = run / "firmware.hex"
    existing.symlink_to(source)

    with materialize_runtime_inputs(build, run, ("firmware.hex",)):
        assert existing.read_text(encoding="utf-8") == "01\n"

    assert existing.is_symlink()
    assert existing.resolve() == source


def test_similarly_named_project_symlink_is_not_owned(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    source = build / "firmware.hex"
    source.write_text("01\n", encoding="utf-8")
    project_overlay = run / ".booley-runtime-inputs"
    project_overlay.mkdir()
    project_input = project_overlay / "firmware.hex"
    project_input.write_text("01\n", encoding="utf-8")
    existing = run / "firmware.hex"
    existing.symlink_to(project_input)

    with materialize_runtime_inputs(build, run, ("firmware.hex",)):
        assert existing.resolve() == project_input

    assert existing.is_symlink()
    assert existing.resolve() == project_input


def test_intermediate_project_symlink_cannot_redirect_staging(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    outside = tmp_path / "outside"
    source = build / "vectors" / "input.hex"
    source.parent.mkdir(parents=True)
    source.write_text("01\n", encoding="utf-8")
    run.mkdir()
    outside.mkdir()
    (run / "vectors").symlink_to(outside, target_is_directory=True)

    with (
        pytest.raises(RuntimeInputError, match="conflicts with an existing path"),
        materialize_runtime_inputs(build, run, ("vectors/input.hex",)),
    ):
        pass

    assert not (outside / "input.hex").exists()


def test_cleanup_failure_is_typed_and_overlay_cleanup_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    (build / "firmware.hex").write_text("01\n", encoding="utf-8")
    staged = run / "firmware.hex"
    original_unlink = Path.unlink

    def fail_staged_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == staged:
            raise OSError("read-only run directory")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_staged_unlink)
    with (
        pytest.raises(RuntimeInputError, match="could not clean simulation runtime inputs"),
        materialize_runtime_inputs(build, run, ("firmware.hex",)),
    ):
        assert staged.is_symlink()

    assert not (build / ".booley-runtime-inputs").exists()


def test_conflicting_project_input_is_not_overwritten(tmp_path: Path) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()
    (build / "firmware.hex").write_text("target\n", encoding="utf-8")
    existing = run / "firmware.hex"
    existing.write_text("project\n", encoding="utf-8")

    with (
        pytest.raises(RuntimeInputError, match="conflicts with an existing path"),
        materialize_runtime_inputs(build, run, ("firmware.hex",)),
    ):
        pass

    assert existing.read_text(encoding="utf-8") == "project\n"


@pytest.mark.parametrize(
    ("missing", "message"),
    (
        ("build", "simulation build directory does not exist"),
        ("run", "simulation run directory does not exist"),
    ),
)
def test_missing_runtime_input_directory_is_rejected(
    tmp_path: Path, missing: str, message: str
) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    if missing != "build":
        build.mkdir()
    if missing != "run":
        run.mkdir()

    with (
        pytest.raises(RuntimeInputError, match=message),
        materialize_runtime_inputs(build, run, ("firmware.hex",)),
    ):
        pass


@pytest.mark.parametrize("relative", ("../outside.hex", "/outside.hex"))
def test_runtime_input_cannot_escape_its_roots(tmp_path: Path, relative: str) -> None:
    build = tmp_path / "build"
    run = tmp_path / "run"
    build.mkdir()
    run.mkdir()

    with (
        pytest.raises(
            RuntimeInputError,
            match=r"invalid build input path|build input path escapes its root",
        ),
        materialize_runtime_inputs(build, run, (relative,)),
    ):
        pass
