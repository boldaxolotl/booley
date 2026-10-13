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


def test_run_evidence_reuses_preflight_source_evidence(tmp_path, monkeypatch):
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
    source = run_evidence.capture_flow_source_evidence(tmp_path, "core")
    monkeypatch.setattr(
        run_evidence,
        "compute_source_fingerprint",
        lambda *_args, **_kwargs: pytest.fail("source evidence was captured twice"),
    )

    evidence = run_evidence.build_flow_run_evidence(
        flow="fpga",
        target="core",
        recipe_sha256="d" * 64,
        work_dir=tmp_path,
        run_id="run-1",
        source_evidence=source,
    )

    assert evidence.source_revision == source.source_revision == "a" * 40
    assert evidence.source_sha256 == source.source_sha256


def test_declared_file_resource_changes_source_evidence_without_parameter_edit(tmp_path):
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    resource = tmp_path / "data.mem"
    resource.write_text("11\n")
    core = tmp_path / "proof.core"
    core.write_text("""CAPI=2:
name: proof:string:file:1
filesets:
  rtl:
    files: [top.sv]
    file_type: systemVerilogSource
  data:
    files: [data.mem]
    file_type: user
parameters:
  FILE: {datatype: file, paramtype: vlogparam, default: data.mem}
targets:
  synth:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}
    filesets: [rtl, data]
    parameters: [FILE]
    toplevel: top
""")
    before = run_evidence.capture_flow_source_evidence(tmp_path, "synth")
    definition = core.read_bytes()
    resource.write_text("a5\n")
    after = run_evidence.capture_flow_source_evidence(tmp_path, "synth")
    assert after.source_sha256 != before.source_sha256
    assert core.read_bytes() == definition
