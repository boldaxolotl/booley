from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.campaign.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.execution.contract import SimulationPreview
from booley.flows.sim.flow import SimulateFlow
from booley.flows.test_runs import build_test_run
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
    assert catalog_empty["required_suite"]["source_path"] == ".booley/tests.toml"  # type: ignore[index]
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


def test_invalid_run_cwd_template_is_refused_before_a_manifest_exists(tmp_path: Path) -> None:
    """Planning rejects a bad template; no plan means nothing can be published."""
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "runs/{bogus}"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"invalid \[flows\.sim\]\.run_cwd template"):
        _plan(handle, inspection, preview, ("reset", "count"))


@pytest.mark.parametrize(
    "value",
    ['"make -C tests build"', '["make", 3]'],
    ids=["bare-string", "non-string-entry"],
)
def test_invalid_pre_run_commands_are_refused_before_a_manifest_exists(
    tmp_path: Path, value: str
) -> None:
    """Planning rejects a knob that is not a list of strings; nothing is frozen."""
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    (project / "booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = {value}\n", encoding="utf-8"
    )

    with pytest.raises(
        ValueError, match=r"\[flows\.sim\]\.pre_run_commands must be a list of strings"
    ):
        _plan(handle, inspection, preview, ("reset", "count"))


def _planning_fixture(tmp_path: Path):
    project = tmp_path / ".booley"
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


def test_source_recipe_ignores_flow_options_defines(tmp_path):
    """No Flow applies `flow_options.defines`, so the recipe must not record it."""
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    baseline = _plan(handle, inspection, preview, ("reset", "count"))
    baseline_recipe = baseline["workload"]["source_recipe"]
    assert baseline_recipe["defines"] == ()
    baseline_command = baseline["workload"]["build_recipe"]["command_model_sha256"]
    for shape in ({"A": 1}, ["A", "B=2"], "A"):
        planned = dataclasses.replace(inspection, flow_options={"defines": shape})
        document = _plan(handle, planned, preview, ("reset", "count"))
        assert document["workload"]["source_recipe"] == baseline_recipe
        assert document["workload"]["source_recipe"]["defines"] == ()
        # The opaque whole-mapping identity still covers every flow_options key.
        assert document["workload"]["build_recipe"]["command_model_sha256"] != baseline_command


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_campaign_baseline_planning_materializes_project_topologies(
    tmp_path, monkeypatch, topology
):
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_cycle_observation import _criterion_flow, _pin_baseline
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    handle = TargetCatalog.build(root).select("sim_core", for_flow="sim")
    flow._target_handles = {"sim_core": handle}
    _pin_baseline(flow, revision)
    roots = []
    request = SimpleNamespace(revision=revision)
    prerequisite = {"revision": revision, "target_identity": handle.identity}

    def plan(target, identity, ref, requirements, tests, worktree, invocation, admission):
        _assert_topology_baseline(worktree, root, target)
        assert identity == handle.identity
        assert ref == revision
        assert [run.name for run in requirements] == ["coremark@1"]
        assert tests == {"sim_core": ["coremark"]}
        assert invocation == tmp_path / "invocation"
        assert admission is None
        assert flow.args.work_dir == worktree
        roots.append(worktree)
        return request, {"coremark@1": prerequisite}

    monkeypatch.setattr(flow, "_planned_baseline_request", plan)
    try:
        requests, prerequisites = flow._plan_campaign_baselines(
            tmp_path / "invocation", None, ["sim_core"], {"sim_core": ["coremark"]}
        )
        assert requests == [request]
        assert prerequisites == {"sim_core": [prerequisite]}
        assert flow.args.work_dir == root
        assert roots and roots[0].exists()
    finally:
        flow.publication_resources.close()
    assert not roots[0].exists()


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_campaign_baseline_preview_materializes_project_topologies(
    tmp_path, monkeypatch, topology
):
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_cycle_observation import _criterion_flow, _pin_baseline
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    handle = TargetCatalog.build(root).select("sim_core", for_flow="sim")
    flow._target_handles = {"sim_core": handle}
    _pin_baseline(flow, revision)
    roots = []
    campaign = object()
    prerequisite = {"revision": revision, "target_identity": handle.identity}
    preview = {"target": handle.identity}

    def inspect(owner, target, ref, requirements, tests, worktree):
        assert owner is campaign
        _assert_topology_baseline(worktree, root, target)
        assert ref == revision
        assert [run.name for run in requirements] == ["coremark@1"]
        assert tests == {"sim_core": ["coremark"]}
        assert flow.args.work_dir == worktree
        roots.append(worktree)
        return preview, {"coremark@1": prerequisite}

    monkeypatch.setattr(flow, "_preview_one_campaign_baseline", inspect)
    with flow.publication_resources:
        prerequisites, previews = flow._preview_campaign_baselines(
            campaign, ["sim_core"], {"sim_core": ["coremark"]}
        )
        assert previews == [preview]
        assert prerequisites == {"sim_core": [prerequisite]}
        assert flow.args.work_dir == root
    assert roots and not roots[0].exists()


@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize("contract_version", ["1", "2"])
@pytest.mark.parametrize("raw_coverage", [False, True])
def test_every_planned_manifest_is_v4_whatever_it_discloses(
    mapping, raw_coverage, contract_version
):
    from booley.flows.sim.campaign.flow_planning import _manifest_document
    from tests.flows.sim.test_campaign_manifest_codec import _manifest

    document = _manifest()
    workload = document["workload"]
    workload["coverage"] = raw_coverage
    if raw_coverage:
        workload["no_waivers"] = True
    entry = {
        "path": "staged.hex",
        "bytes": 1,
        "sha256": "sha256:" + "a" * 64,
        "kind": "generated_input",
    }
    if mapping:
        entry["source_path"] = "data/source.hex"
    disclosure = {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [entry],
        "tool_provenance": {
            "kind": "fusesoc",
            "version": "2",
            "contract_version": contract_version,
        },
        "cleanup": {"removed": True},
    }
    from booley.flows.sim.campaign.flow_planning import _variant_document, _work_items

    variant = _variant_document(workload, [], False)
    variants, items = (
        [variant],
        _work_items(
            [(build_test_run(None),)],
            document["target"],
            variant,
            "run",
            "literal",
            kind="ordinary_hdl",
        ),
    )
    document["build_variants"] = variants
    document["work_items"] = items
    # This boundary chooses the schema; finalization recalculates the fingerprints.
    manifest = _manifest_document(
        1,
        "",
        document["target"],
        workload,
        document["required_suite"],
        document["build_variants"][0],
        [disclosure],
        [],
        document["work_items"],
    )
    # One written version: it records each work item's Test Runs (ADR 0072),
    # permits source identities, and carries no_waivers only when requested.
    assert manifest.document["$schema"] == "booley.simulation-campaign-manifest/v4"
    assert ("no_waivers" in manifest.document["workload"]) is raw_coverage
    assert [run["name"] for run in manifest.document["work_items"][0]["test_runs"]] == [
        "<default>"
    ]


def test_empty_target_toplevel_is_refused_at_planning(tmp_path):
    from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError

    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    with pytest.raises(SimulationCampaignIntegrityError, match="empty toplevel; set toplevel"):
        _plan(handle, dataclasses.replace(inspection, toplevel=""), preview, ("reset", "count"))


def test_copyto_destination_outside_the_run_directory_is_refused_at_planning(tmp_path):
    from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError

    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    (tmp_path / "fw.hex").write_text("00\n", encoding="utf-8")
    staged = TargetInput("fw.hex", "acme:lib:dut:1", "user", (), False, {"copyto": "C:fw.hex"})
    planned = dataclasses.replace(inspection, inputs=(*inspection.inputs, staged))
    with pytest.raises(SimulationCampaignIntegrityError, match=r"copyto destination 'C:fw\.hex'"):
        _plan(handle, planned, preview, ("reset", "count"))


def test_empty_pre_run_command_is_refused_at_planning(tmp_path):
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    (project / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["make", ""]\n', encoding="utf-8"
    )
    with pytest.raises(
        ValueError, match=r"pre_run_commands must be a list of strings, none empty"
    ):
        _plan(handle, inspection, preview, ("reset", "count"))
