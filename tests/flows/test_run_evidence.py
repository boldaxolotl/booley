"""Tests for compact EDA run provenance."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.flows import run_evidence


def test_run_evidence_records_only_artifact_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evidence, "git_full_sha", lambda *_args: "a" * 40)

    evidence = run_evidence.build_flow_run_evidence(
        flow="synth",
        target="core",
        recipe_sha256="d" * 64,
        source_sha256="b" * 64,
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


def test_resolved_input_digest_uses_staged_bytes(tmp_path):
    from booley.fusesoc.fusesoc_registry import ResolvedFile

    live = tmp_path / "worktree" / "rtl.sv"
    live.parent.mkdir()
    live.write_text("module live; endmodule\n", encoding="utf-8")
    staged = tmp_path / "src" / "rtl.sv"
    staged.parent.mkdir()
    staged.write_text("module staged; endmodule\n", encoding="utf-8")
    resolved = ResolvedFile(name="src/rtl.sv", file_type="systemVerilogSource")

    first = run_evidence.digest_resolved_inputs((resolved,), tmp_path)
    live.write_text("module changed_live; endmodule\n", encoding="utf-8")
    assert run_evidence.digest_resolved_inputs((resolved,), tmp_path) == first

    staged.write_text("module changed; endmodule\n", encoding="utf-8")
    second = run_evidence.digest_resolved_inputs((resolved,), tmp_path)

    assert first != second


def test_flow_run_evidence_boundary_parser_rejects_invalid_records():
    valid = run_evidence.FlowRunEvidence(
        run_id="run",
        source_revision="revision",
        source_sha256="source",
        recipe_sha256="recipe",
    )

    assert run_evidence.FlowRunEvidence.from_dict(valid.as_dict()) == valid
    assert run_evidence.FlowRunEvidence.from_dict({"version": True}) is None
