"""Simulation execution artifact-integrity tests."""

from pathlib import Path

import pytest

from booley.flows.sim.execution.freshness import (
    ArtifactValidationError,
    snapshot_artifact,
    validate_fresh_artifact,
)


def test_new_artifact_inside_attempt_root_is_accepted(tmp_path: Path) -> None:
    artifact = tmp_path / "attempt" / "wave.fst"
    artifact.parent.mkdir()
    artifact.write_bytes(b"wave")

    evidence = validate_fresh_artifact(artifact, roots=(artifact.parent,), before=None)

    assert evidence.path == artifact.resolve()
    assert evidence.size == 4


def test_unchanged_artifact_is_rejected_as_stale(tmp_path: Path) -> None:
    artifact = tmp_path / "wave.fst"
    artifact.write_bytes(b"old")
    before = snapshot_artifact(artifact)

    with pytest.raises(ArtifactValidationError, match="stale"):
        validate_fresh_artifact(artifact, roots=(tmp_path,), before=before)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    outside = tmp_path / "outside.fst"
    outside.write_bytes(b"wave")
    link = root / "wave.fst"
    link.symlink_to(outside)

    with pytest.raises(ArtifactValidationError, match="escapes"):
        validate_fresh_artifact(link, roots=(root,), before=None)


def test_configured_absolute_artifact_may_be_explicitly_allowed(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    external = tmp_path / "configured" / "wave.fst"
    external.parent.mkdir()
    external.write_bytes(b"wave")

    evidence = validate_fresh_artifact(
        external,
        roots=(root,),
        before=None,
        explicitly_allowed=(external,),
    )

    assert evidence.path == external.resolve()
