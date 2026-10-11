"""Simulation Campaign manifests record each work item's Test Runs (ADR 0072)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.flow_planning import (
    plan_coarse_simulation_campaign,
    plan_ordinary_hdl_campaign,
)
from booley.flows.sim.campaign.planning import compare_manifests, finalize_manifest
from booley.flows.sim.campaign.serial_execution import _work_item_test_runs
from booley.flows.sim.flow import SimulateFlow, _manifest_run_groups
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import TestRun, build_test_run, test_run_document
from tests.flows.sim import test_campaign_resume_diagnostics as resume_support
from tests.flows.sim.test_campaign_flow_planning import _planning_fixture
from tests.flows.sim.test_campaign_manifest_codec import _manifest as _v1_manifest

_V4 = "booley.simulation-campaign-manifest/v4"


def _hdl_plan(
    tmp_path: Path,
    run_groups: tuple[tuple[TestRun, ...], ...],
    required: tuple[str, ...] = ("reset", "count"),
):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    groups = tuple(
        tuple(run.test for run in group if run.test is not None) for group in run_groups
    )
    preview = replace(
        preview,
        groups=groups,
        run_groups=run_groups,
        commands=tuple(("run",) for _ in run_groups),
    )
    return plan_ordinary_hdl_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        groups=groups,
        required_suite=required,
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=False,
    )


def _cocotb_plan(tmp_path: Path, run_groups: tuple[tuple[TestRun, ...], ...]):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    inspection = replace(inspection, flow_options={"cocotb_module": "tests"})
    groups = tuple(tuple(run.test for run in group) for group in run_groups)
    preview = replace(
        preview,
        groups=groups,
        run_groups=run_groups,
        commands=tuple(("run",) for _ in run_groups),
        flow_options={"cocotb_module": "tests"},
    )
    return plan_coarse_simulation_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=tuple(name for group in groups for name in group),
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=False,
        kind="cocotb_batch",
    )


_SOAK = build_test_run("count", variant="soak", seed=42, args=("+iters=9",))
_FAULT = build_test_run("count", variant="fault", seed=42, env={"FAULT": "1"})
_OTHER_SEED = build_test_run("count", variant="soak", seed=43, args=("+iters=9",))


def test_hdl_test_runs_that_differ_each_get_their_own_work_item(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), _SOAK, _FAULT, _OTHER_SEED)
    document = _hdl_plan(tmp_path, tuple((run,) for run in runs)).manifest.document
    items = document["work_items"]
    assert document["$schema"] == _V4
    assert [item["selection"]["names"] for item in items] == [
        ("reset",),
        ("count",),
        ("count",),
        ("count",),
    ]
    assert [[run["name"] for run in item["test_runs"]] for item in items] == [
        ["reset@1"],
        ["count+soak@42"],
        ["count+fault@42"],
        ["count+soak@43"],
    ]
    assert items[1]["test_runs"][0] == {
        "name": "count+soak@42",
        "test": "count",
        "variant": "soak",
        "seed": 42,
        "args": ("+iters=9",),
        "env": {},
        "fingerprint": _SOAK.fingerprint,
    }
    # The Test Run is part of the work item's identity, never of the build's.
    assert len({item["work_item_id"] for item in items}) == 4
    assert len({item["build_variant_id"] for item in items}) == 1
    assert len(document["build_variants"]) == 1


def test_work_item_test_runs_round_trip_to_the_same_values(tmp_path: Path):
    plan = _hdl_plan(tmp_path, ((_SOAK,), (_FAULT,)))
    raw = encode_simulation_campaign_manifest(plan.manifest)
    manifest = decode_simulation_campaign_manifest(raw)
    assert _manifest_run_groups(manifest) == ((_SOAK,), (_FAULT,))
    assert _work_item_test_runs(manifest.document["work_items"][1]) == (_FAULT,)
    assert encode_simulation_campaign_manifest(manifest) == raw


def test_unnamed_invocation_records_its_seed(tmp_path: Path):
    document = _hdl_plan(tmp_path, ((build_test_run(None, seed=7),),), ()).manifest.document
    item = document["work_items"][0]
    assert item["selection"] == {"kind": "default", "names": ()}
    assert [run["name"] for run in item["test_runs"]] == ["<default>@7"]


def test_hdl_work_item_refuses_more_than_one_test_run(tmp_path: Path):
    with pytest.raises(SimulationCampaignIntegrityError, match="planned groups and Test Runs"):
        _hdl_plan(tmp_path, ((_SOAK, _OTHER_SEED),))


def test_cocotb_default_suite_is_one_batch(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    items = _cocotb_plan(tmp_path, (runs,)).manifest.document["work_items"]
    assert [item["kind"] for item in items] == ["cocotb_batch"]
    assert items[0]["selection"]["names"] == ("reset", "count")
    assert [run["name"] for run in items[0]["test_runs"]] == ["reset@1", "count@1"]


def test_cocotb_batches_split_by_environment_and_seed(tmp_path: Path):
    groups = (
        (build_test_run("reset", seed=1), build_test_run("count", seed=1)),
        (build_test_run("count", variant="slow", seed=1, env={"SPEED": "slow"}),),
        (build_test_run("reset", seed=5),),
    )
    items = _cocotb_plan(tmp_path, groups).manifest.document["work_items"]
    assert [item["kind"] for item in items] == ["cocotb_batch"] * 3
    assert [[run["name"] for run in item["test_runs"]] for item in items] == [
        ["reset@1", "count@1"],
        ["count+slow@1"],
        ["reset@5"],
    ]
    assert len({item["build_variant_id"] for item in items}) == 1


def _v4_document(tmp_path: Path) -> dict:
    plan = _hdl_plan(tmp_path, ((_SOAK,),))
    return json.loads(encode_simulation_campaign_manifest(plan.manifest))


def _refinalized(document: dict):
    """Finalize *document* again so only the altered field can be refused."""
    document.pop("fingerprints")
    return finalize_manifest(document)


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        (lambda item: item.pop("test_runs"), "exact fields"),
        (lambda item: item["test_runs"].clear(), "nonempty and uniquely named"),
        (lambda item: item["test_runs"].append(copy.deepcopy(item["test_runs"][0])), "uniquely"),
        (lambda item: item["test_runs"][0].update(name="count+soak@43"), "disagrees with its own"),
        (lambda item: item["test_runs"][0].update(args=["+iters=1"]), "disagrees with its own"),
        (lambda item: item["test_runs"][0].update(seed=0), "out of range"),
        (lambda item: item["test_runs"][0].update(extra=1), "must hold exactly"),
        (lambda item: item["test_runs"][0].pop("env"), "must hold exactly"),
        (
            lambda item: item["test_runs"].__setitem__(
                0, test_run_document(build_test_run("reset", seed=42))
            ),
            "disagree with selection",
        ),
        (
            lambda item: item["test_runs"].append(test_run_document(_OTHER_SEED)),
            "ordinary work item requires one Test Run",
        ),
    ],
)
def test_v4_refuses_a_work_item_whose_test_runs_are_wrong(tmp_path: Path, alter, message):
    document = _v4_document(tmp_path)
    alter(document["work_items"][0])
    with pytest.raises(SimulationCampaignIntegrityError, match=message):
        _refinalized(document)


def test_cocotb_batch_refuses_test_runs_that_differ_in_seed(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    document = json.loads(
        encode_simulation_campaign_manifest(_cocotb_plan(tmp_path, (runs,)).manifest)
    )
    document["work_items"][0]["test_runs"][1] = test_run_document(build_test_run("count", seed=2))
    with pytest.raises(SimulationCampaignIntegrityError, match="share seed and environment"):
        _refinalized(document)


def test_a_manifest_before_v4_may_not_carry_test_runs(tmp_path: Path):
    document = _v4_document(tmp_path)
    document["$schema"] = "booley.simulation-campaign-manifest/v3"
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        _refinalized(document)


def test_a_manifest_before_v4_still_decodes_for_reports():
    manifest = finalize_manifest({k: v for k, v in _v1_manifest().items() if k != "fingerprints"})
    decoded = decode_simulation_campaign_manifest(encode_simulation_campaign_manifest(manifest))
    assert decoded.document["$schema"] == "booley.simulation-campaign-manifest/v1"
    assert all("test_runs" not in item for item in decoded.document["work_items"])
    assert _work_item_test_runs(decoded.document["work_items"][0]) is None


def test_resuming_a_manifest_before_v4_fails_closed():
    manifest = finalize_manifest({k: v for k, v in _v1_manifest().items() if k != "fingerprints"})
    with pytest.raises(SimulationCampaignIntegrityError) as excinfo:
        _manifest_run_groups(manifest)
    assert str(excinfo.value) == (
        "this Simulation Campaign manifest predates recorded Test Runs "
        "(booley.simulation-campaign-manifest/v1) and cannot be resumed; "
        "start a new Simulation Campaign"
    )


@pytest.mark.parametrize(
    "fields",
    [{"seed": "random"}, {"test": ("reset@x3",)}, {"test": ("wrap@9", "reset")}],
    ids=["seed-random", "count", "pinned"],
)
def test_interrupted_campaign_resumes_with_byte_identical_test_runs(tmp_path, monkeypatch, fields):
    """Resume re-plans from the manifest's own Test Runs: no seed is drawn again."""
    reports, manifest, executions = _interrupted(tmp_path, monkeypatch, fields)
    published = json.loads(manifest.read_bytes())
    recorded = [item["test_runs"] for item in published["work_items"]]
    seeds = [run["seed"] for runs in recorded for run in runs]
    assert all(isinstance(seed, int) and seed >= 1 for seed in seeds)
    if fields == {"test": ("reset@x3",)}:
        assert len(set(seeds)) == 3
    pairs = resume_support._capture_resume_comparison(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        SimulateFlow().execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
        )

    assert len(executions) == 2, "resume must reach execution, not a mismatch refusal"
    (expected, current), *_rest = pairs
    assert compare_manifests(expected, current) == ()
    replanned = json.loads(encode_simulation_campaign_manifest(current))["work_items"]
    assert json.dumps([item["test_runs"] for item in replanned], sort_keys=True) == json.dumps(
        recorded, sort_keys=True
    )
    assert json.loads(manifest.read_bytes()) == published


def _interrupted(root, monkeypatch, fields):
    """An ordinary campaign of Target ``sim_0`` interrupted in its first work item."""
    original = SimRequest

    def request(**kwargs):
        if "resume_from" not in kwargs:
            kwargs.update(fields)
        return original(**kwargs)

    monkeypatch.setattr(resume_support, "SimRequest", request)
    try:
        return resume_support._interrupted_campaign(root, monkeypatch)
    finally:
        monkeypatch.setattr(resume_support, "SimRequest", original)
