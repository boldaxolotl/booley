from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.campaign.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.execution.contract import SimulationPreview
from booley.flows.sim.flow import SimulateFlow
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection


def test_resolved_target_plans_one_private_serial_item_per_exact_test(
    tmp_path: Path,
) -> None:
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    document = _plan(handle, inspection, preview, ("reset", "count"))
    assert "build_timeout" not in repr(document)
    assert [item["selection"]["names"] for item in document["work_items"]] == [  # type: ignore[index]
        ("reset",),
        ("count",),
    ]
    assert document["required_suite"]["names"] == ("reset", "count")  # type: ignore[index]
    assert document["workload"]["run_cwd"]["configured"] == "."  # type: ignore[index]
    assert document["workload"]["eda"]["kind"] == "icarus"  # type: ignore[index]
    assert all(item["kind"] == "ordinary_hdl" for item in document["work_items"])  # type: ignore[union-attr]
    catalog_empty = _plan(handle, inspection, preview, (), catalog_backed=True)
    assert catalog_empty["required_suite"]["names"] == ()  # type: ignore[index]
    assert catalog_empty["required_suite"]["default_invocation"] is False  # type: ignore[index]
    assert catalog_empty["required_suite"]["source_path"] == ".booley_project/tests.toml"  # type: ignore[index]
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "."\npre_sim_build_access = "legacy-per-test"\n',
        encoding="utf-8",
    )
    private = _plan(handle, inspection, preview, ("reset", "count"))
    assert private["build_variants"][0]["sharing_eligible"] is False  # type: ignore[index]


def test_candidate_and_baseline_plans_disclose_carried_build_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, handle, _inspection, preview = _planning_fixture(tmp_path)
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim"])
    flow._target_handles = {"sim": handle}
    monkeypatch.setattr(flow, "_effective_timeout_ms", lambda: 5000)
    monkeypatch.setattr(flow, "_effective_build_timeout_ms", lambda: 7000)
    monkeypatch.setattr(flow, "_target_sim_env", lambda _target: {})
    monkeypatch.setattr(flow, "_redact_plan_environment", lambda _target, command: command)

    candidate = flow._simulation_work_unit(
        "sim", preview, ("reset",), ("run", "reset"), "candidate", None
    )
    baseline = flow._simulation_work_unit(
        "sim", preview, ("reset",), ("run", "reset"), "baseline", "abc123"
    )

    assert candidate.timeout_ms == baseline.timeout_ms == 5000
    assert candidate.recipe["build_timeout_ms"] == 7000
    assert baseline.recipe["build_timeout_ms"] == 7000


def _planning_fixture(tmp_path: Path):
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text("[flows.sim]\nrun_cwd = '.'\n", encoding="utf-8")
    (project / "tests.toml").write_text('[sim]\ntests = ["reset", "count"]\n', encoding="utf-8")
    source = tmp_path / "tb.sv"
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    handle = cast(
        TargetHandle,
        SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            selector="sim",
            name="sim",
            vlnv="acme:lib:dut:1",
            project_root=tmp_path.resolve(),
            flow=None,
            eda_tool=None,
        ),
    )
    inputs = (TargetInput("tb.sv", "acme:lib:dut:1", "systemVerilogSource", ("tb",), False, {}),)
    inspection = TargetInspection(
        handle,
        "tb",
        "sim",
        "icarus",
        {},
        {},
        inputs,
    )
    preview = SimulationPreview(
        commands=(("run", "reset"), ("run", "count")),
        groups=(("reset",), ("count",)),
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="icarus",
        sources=("tb.sv",),
        constraints=(),
        parameters={},
        flow_options={},
    )

    return project, handle, inspection, preview


def _plan(handle, inspection, preview, required, *, catalog_backed=None):
    return plan_ordinary_hdl_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        groups=preview.groups,
        required_suite=required,
        required_suite_catalog_backed=catalog_backed,
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=False,
    ).manifest.document
