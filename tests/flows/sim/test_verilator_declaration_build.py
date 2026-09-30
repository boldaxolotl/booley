"""Producing-build attribution and failure handling without a compiler process."""

import json
from dataclasses import replace

import pytest

from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSlotError
from booley.flows.sim.coverage_campaign import freeze_coverage_mapping
from booley.flows.sim.coverage_provenance import content_digest
from booley.flows.sim.coverage_source_gaps import GAP_CODE, source_gap_findings
from booley.flows.sim.verilator_declaration_build import capture_build_declarations
from booley.flows.sim.verilator_identity import PINNED_VERILATOR
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.test_verilator_coverage_execution import _write_target
from tests.flows.sim.test_verilator_declarations import _evidence


def _producing_build(root):
    _write_target(root)
    authored = root / "rtl/counter.sv"
    authored.write_text("`define ADD_ONE(x) \\\n ((x) + 1)\nmodule counter; endmodule\n")
    catalog = TargetCatalog.build(root)
    inspection = catalog.inspect(catalog.select("sim", for_flow="sim"))
    build = root / "build/generation"
    staged = build / "src/core/rtl/counter.sv"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(authored.read_bytes())
    edam = build / "target.eda.yml"
    edam.write_text(json.dumps({"cores": {"core": {"core_file": str(root / "counter.core")}}}))
    resolved = ResolvedTarget(
        "sim",
        "core",
        "counter_tb",
        "verilator",
        (
            ResolvedFile("src/core/rtl/counter.sv", "systemVerilogSource", core="core"),
            ResolvedFile("firmware.hex", "user"),
        ),
        {},
        build,
        edam,
    )
    prepared = PreparedSimulationBuild(
        "sim",
        inspection.handle.identity,
        resolved,
        build,
        build,
        "verilator",
        "counter_tb",
        ("make",),
    )
    _tree, meta = _evidence(build, filename="src/core/rtl/counter.sv")
    response = build / "inputs.vc"
    response.write_text("src/core/rtl/counter.sv\n")
    metadata = json.loads(meta.read_text())
    metadata["files"]["r"] = {"filename": "inputs.vc", "realpath": str(response)}
    meta.write_text(json.dumps(metadata))
    return prepared, inspection, {"inputs.vc": content_digest(response.read_bytes())}


def _capture(prepared, inspection, inputs):
    return capture_build_declarations(
        prepared,
        inspection,
        compiler=(PINNED_VERILATOR.tag, PINNED_VERILATOR.commit),
        build_inputs=inputs,
    )


def test_staged_aliases_and_continued_macros_keep_zero_point_source_findings(tmp_path):
    prepared, inspection, inputs = _producing_build(tmp_path)
    inventory = _capture(prepared, inspection, inputs)
    assert inventory.status == "complete"
    source = inventory.sources[0]
    assert source.path == "rtl/counter.sv"
    assert "src/core/rtl/counter.sv" in source.aliases
    assert source.sha256 == content_digest((tmp_path / source.path).read_bytes())
    assert [d.source for d in inventory.declarations] == [source.path]
    closure = freeze_coverage_mapping({"rtl": [{"path": source.path, "sha256": source.sha256}]})
    findings = source_gap_findings(inventory, closure, (), ())
    assert [(f.code, f.pointer) for f in findings] == [(GAP_CODE, "/source_closure/rtl/0/path")]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("missing_dump", "capture_unavailable"),
        ("bad_edam", "capture_unavailable"),
        ("unmapped", "unmapped_input"),
        ("duplicate", "unmapped_input"),
        ("logical_source", "logical_locations"),
        ("logical_options", "logical_options"),
        ("foreign_input", "unenumerated_input"),
        ("logical_response", "unenumerated_input"),
        ("unbound_response", "unenumerated_input"),
    ],
)
def test_capture_preserves_specific_incomplete_discovery_reasons(tmp_path, failure, code):
    prepared, inspection, inputs = _producing_build(tmp_path)
    resolved = prepared.resolved
    if failure == "missing_dump":
        (prepared.build_root / "Custom_023_cells.tree.json").unlink()
    elif failure == "bad_edam":
        resolved.edam_path.write_text("[invalid")
    elif failure == "unmapped":
        resolved.edam_path.write_text(
            json.dumps({"cores": {"core": {"core_file": str(tmp_path.parent / "foreign.core")}}})
        )
    elif failure == "duplicate":
        prepared = replace(prepared, resolved=replace(resolved, files=resolved.files * 2))
    elif failure == "logical_source":
        for path in (tmp_path / "rtl/counter.sv", prepared.build_root / "src/core/rtl/counter.sv"):
            path.write_text('`line 100 "logical.sv" 0\nmodule counter; endmodule\n')
    elif failure == "logical_options":
        prepared = replace(
            prepared, resolved=replace(resolved, flow_options={"defines": {"LOC": "`line"}})
        )
    elif failure == "foreign_input":
        meta = prepared.build_root / "Custom.tree.meta.json"
        metadata = json.loads(meta.read_text())
        metadata["files"]["z"] = {"filename": "foreign.sv", "realpath": "foreign.sv"}
        meta.write_text(json.dumps(metadata))
    elif failure == "logical_response":
        response = prepared.build_root / "inputs.vc"
        response.write_text("`line decoy\n")
        inputs = {"inputs.vc": content_digest(response.read_bytes())}
    elif failure == "unbound_response":
        inputs = {}
    inventory = _capture(prepared, inspection, inputs)
    assert inventory.status == "incomplete"
    assert code in {d.code for d in inventory.diagnostics}


@pytest.mark.parametrize("changed", ["source", "response"])
def test_changed_producing_bytes_raise_build_integrity_error(tmp_path, changed):
    prepared, inspection, inputs = _producing_build(tmp_path)
    path = prepared.build_root / (
        "src/core/rtl/counter.sv" if changed == "source" else "inputs.vc"
    )
    path.write_text("changed\n")
    with pytest.raises(SimulationBuildSlotError, match="changed"):
        _capture(prepared, inspection, inputs)
