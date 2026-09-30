"""Tests for Simulation trace artifact authorization and freshness."""

from pathlib import Path

import pytest

from booley.flows.sim.execution.artifacts import TraceArtifactPolicy
from booley.flows.sim.execution.freshness import ArtifactValidationError


def test_absolute_hidden_trace_glob_is_authorized(tmp_path: Path) -> None:
    run_cwd = tmp_path / "run"
    build_root = tmp_path / "build"
    hidden_traces = tmp_path / ".traces"
    run_cwd.mkdir()
    build_root.mkdir()
    hidden_traces.mkdir()
    trace = hidden_traces / "wave.fst"
    policy = TraceArtifactPolicy.capture(
        run_cwd=run_cwd,
        build_root=build_root,
        patterns=(str(hidden_traces / "*.fst"),),
    )

    trace.write_bytes(b"waveform")

    assert policy.validate_reported(str(trace)).path == trace


def test_recursive_trace_glob_rejects_symlink_escape(tmp_path: Path) -> None:
    run_cwd = tmp_path / "run"
    build_root = tmp_path / "build"
    outside = tmp_path / "outside"
    run_cwd.mkdir()
    build_root.mkdir()
    outside.mkdir()
    trace = outside / "wave.fst"
    trace.write_bytes(b"waveform")
    link = run_cwd / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    policy = TraceArtifactPolicy.capture(
        run_cwd=run_cwd,
        build_root=build_root,
        patterns=("**/*.fst",),
    )

    with pytest.raises(ArtifactValidationError, match="symbolic link"):
        policy.validate_reported(str(link / "wave.fst"))


def test_in_root_trace_symlink_cannot_bypass_freshness(tmp_path: Path) -> None:
    run_cwd = tmp_path / "run"
    build_root = tmp_path / "build"
    run_cwd.mkdir()
    build_root.mkdir()
    target = run_cwd / "old.data"
    target.write_bytes(b"stale waveform")
    trace = run_cwd / "wave.fst"
    try:
        trace.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    policy = TraceArtifactPolicy.capture(
        run_cwd=run_cwd,
        build_root=build_root,
        patterns=(),
    )

    with pytest.raises(ArtifactValidationError, match="symbolic link"):
        policy.validate_reported(str(trace))


def _policy_with_old(tmp_path: Path, *old: str) -> tuple[Path, Path, TraceArtifactPolicy]:
    run_cwd = tmp_path / "run"
    build_root = tmp_path / "build"
    run_cwd.mkdir()
    build_root.mkdir()
    for name in old:
        (run_cwd / name).write_bytes(b"old")
    policy = TraceArtifactPolicy.capture(run_cwd=run_cwd, build_root=build_root, patterns=())
    return run_cwd.resolve(), build_root.resolve(), policy


def test_fresh_candidates_list_new_and_changed_but_not_unchanged(tmp_path: Path) -> None:
    run_cwd, build_root, policy = _policy_with_old(tmp_path, "same.vcd", "changed.vcd")
    (run_cwd / "changed.vcd").write_bytes(b"much longer content")
    (run_cwd / "new.fst").write_bytes(b"x")
    (build_root / "b.vcd").write_bytes(b"x")

    listed, omitted = policy.fresh_candidates(owned=frozenset(), ignored_dirs=(), limit=10)

    assert listed == (build_root / "b.vcd", run_cwd / "changed.vcd", run_cwd / "new.fst")
    assert omitted == 0


def test_fresh_candidates_drop_owned_paths_and_apply_limit(tmp_path: Path) -> None:
    run_cwd, _build_root, policy = _policy_with_old(tmp_path)
    for name in ("a.vcd", "b.vcd", "c.vcd", "own.vcd"):
        (run_cwd / name).write_bytes(b"x")

    listed, omitted = policy.fresh_candidates(
        owned=frozenset({run_cwd / "own.vcd"}), ignored_dirs=(), limit=2
    )

    assert listed == (run_cwd / "a.vcd", run_cwd / "b.vcd")
    assert omitted == 1


def test_fresh_candidates_ignored_dir_applies_only_outside_walked_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    build_root = root / "state" / "build"
    build_root.mkdir(parents=True)
    (root / "state" / "other").mkdir()
    (root / "state" / "other" / "x.vcd").write_bytes(b"x")
    (build_root / "own.vcd").write_bytes(b"x")
    policy = TraceArtifactPolicy.capture(run_cwd=root, build_root=build_root, patterns=())
    (root / "state" / "other" / "y.vcd").write_bytes(b"x")
    (build_root / "own2.vcd").write_bytes(b"x")

    listed, _ = policy.fresh_candidates(
        owned=frozenset(), ignored_dirs=(root / "state",), limit=10
    )

    # The build root lives inside the ignored dir, so it is still walked; the
    # run cwd walk skips the ignored dir and never reports "other".
    assert listed == (build_root / "own2.vcd",)


def test_fresh_candidates_skip_symlink_escape(tmp_path: Path) -> None:
    run_cwd, _build_root, policy = _policy_with_old(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "w.vcd").write_bytes(b"x")
    try:
        (run_cwd / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    assert policy.fresh_candidates(owned=frozenset(), ignored_dirs=(), limit=5) == ((), 0)
