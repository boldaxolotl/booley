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

    with pytest.raises(ArtifactValidationError, match="escapes"):
        policy.validate_reported(str(link / "wave.fst"))
