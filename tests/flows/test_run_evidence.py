"""Tests for compact EDA run provenance."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.flows import run_evidence


def test_run_evidence_records_only_artifact_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evidence, "git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        run_evidence,
        "compute_source_fingerprint",
        lambda *_args, **_kwargs: {
            "algorithm": "sha256",
            "rtl": {"digest": "b" * 64},
            "tb": {"digest": "c" * 64},
        },
    )

    evidence = run_evidence.build_flow_run_evidence(
        flow="synth",
        target="core",
        recipe_sha256="d" * 64,
        work_dir=tmp_path,
        run_id="run-1",
    )

    assert evidence.run_id == "run-1"
    assert evidence.source_revision == "a" * 40
    assert evidence.recipe_sha256 == "d" * 64
    assert set(evidence.as_dict()) == {
        "version",
        "run_id",
        "source_revision",
        "source_sha256",
        "recipe_sha256",
    }
    with pytest.raises(FrozenInstanceError):
        evidence.run_id = "other"  # type: ignore[misc]
