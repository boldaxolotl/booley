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


def _run_campaign(tmp_path: Path, plan):
    """Run *plan* to terminal acceptance facts without any EDA tool."""
    from booley.flows.sim.campaign import (
        CampaignPolicy,
        NewCampaignRunRequest,
        SimulationCampaign,
    )
    from tests.flows.sim.test_campaign_phase6 import _admission, _NoEdaExecutor

    return SimulationCampaign(_NoEdaExecutor()).run(
        NewCampaignRunRequest(
            plan=plan,
            project_root=tmp_path,
            report_root=None,
            policy=CampaignPolicy(),
            invocation_directory=tmp_path / "reports" / "sim" / "1",
            admission=_admission(),
        )
    )


def test_three_seeds_and_two_variants_of_one_test_reach_acceptance_facts(tmp_path: Path):
    """``count@x3`` plus two Test Variants: five results, none overwrites another."""
    runs = (
        build_test_run("count", seed=3),
        build_test_run("count", seed=4),
        build_test_run("count", seed=5),
        _SOAK,
        _FAULT,
    )
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))

    document = outcome.acceptance_facts.document
    assert document["$schema"] == "booley.simulation-acceptance-facts/v2"
    observations = document["observations"]
    assert [item["test"] for item in observations] == ["count"] * 5
    assert [item["test_run"] for item in observations] == [run.name for run in runs]
    assert len({item["work_item_id"] for item in observations}) == 5
    assert len(document["consumed_results"]) == 5
    assert [item["test_run"] for item in outcome.observations] == [run.name for run in runs]


def test_unnamed_invocation_observation_is_named_by_its_test_run(tmp_path: Path):
    plan = _hdl_plan(tmp_path, ((build_test_run(None, seed=7),),), ())
    observations = _run_campaign(tmp_path, plan).acceptance_facts.document["observations"]
    assert [(item["test"], item["test_run"]) for item in observations] == [(None, "<default>@7")]


def test_tests_an_unfiltered_cocotb_batch_discovered_carry_its_seed():
    from booley.flows.sim.campaign.coordinator import _observation_test_runs

    item = {"test_runs": (test_run_document(build_test_run(None, seed=9)),)}
    observed = ({"test": "found_b"}, {"test": "found_a"})
    assert _observation_test_runs(item, observed) == ("found_b@9", "found_a@9")
    unseeded = {"test_runs": (test_run_document(build_test_run(None)),)}
    assert _observation_test_runs(unseeded, observed) == ("found_b", "found_a")
    assert _observation_test_runs({}, observed) is None


def test_an_observation_without_a_test_run_in_its_work_item_is_refused():
    from booley.flows.sim.campaign.coordinator import _observation_test_runs

    item = {"test_runs": (test_run_document(_SOAK),)}
    with pytest.raises(SimulationCampaignIntegrityError, match="has no Test Run"):
        _observation_test_runs(item, ({"test": "reset"},))
    with pytest.raises(SimulationCampaignIntegrityError, match="has no Test Run"):
        _observation_test_runs(item, ({"test": "count"}, {"test": "count"}))


def test_acceptance_indexes_by_test_run_and_never_lets_one_run_stand_for_another(
    tmp_path: Path,
):
    from booley.flows.sim.acceptance import (
        _candidate_cycle_observations,
        _candidate_observations,
        _candidate_simulation_observations,
    )

    runs = (build_test_run("reset", seed=1), _SOAK, _FAULT)
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))
    facts = outcome.acceptance_facts.document

    by_run = _candidate_observations(facts, outcome.target)
    assert list(by_run) == ["reset@1", "count+soak@42", "count+fault@42"]
    assert [item["test"] for item in by_run.values()] == ["reset", "count", "count"]
    for indexer in (_candidate_simulation_observations, _candidate_cycle_observations):
        with pytest.raises(SimulationCampaignIntegrityError) as excinfo:
            indexer(facts, outcome.target)
        assert str(excinfo.value) == (
            "Test 'count' has several Test Runs in this campaign (count+soak@42, "
            "count+fault@42); a Criterion that names the Test cannot choose between them"
        )


def test_acceptance_keeps_indexing_one_test_run_per_test_by_test(tmp_path: Path):
    from booley.flows.sim.acceptance import _candidate_simulation_observations

    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))
    indexed = _candidate_simulation_observations(outcome.acceptance_facts.document, outcome.target)
    assert list(indexed) == ["reset", "count"]
