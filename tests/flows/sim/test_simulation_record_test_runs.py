"""Simulation Campaign manifests record each work item's Test Runs (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.flow_resume import _manifest_run_groups
from booley.flows.sim.record.codec import (
    MAX_OBSERVATIONS,
    MAX_WORK_ITEMS,
    SimulationRecordIntegrityError,
    decode_simulation_record_manifest,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from booley.flows.sim.record.planning import compare_manifests
from booley.flows.sim.record.test_run_binding import work_item_test_runs
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import (
    TestRun,
    TestRunLimitError,
    build_test_run,
    test_run_document,
)
from tests.flows.sim import test_simulation_record_resume_diagnostics as resume_support
from tests.flows.sim.test_simulation_record_flow_planning import _planning_fixture

_V4 = "booley.simulation-record-manifest/v1"


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
    return plan_ordinary_hdl_simulation_record(
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
    return plan_coarse_simulation_record(
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
    raw = encode_simulation_record_manifest(plan.manifest)
    manifest = decode_simulation_record_manifest(raw)
    assert _manifest_run_groups(manifest) == ((_SOAK,), (_FAULT,))
    assert work_item_test_runs(manifest.document["work_items"][1]) == (_FAULT,)
    assert encode_simulation_record_manifest(manifest) == raw


def test_unnamed_invocation_records_its_seed(tmp_path: Path):
    document = _hdl_plan(tmp_path, ((build_test_run(None, seed=7),),), ()).manifest.document
    item = document["work_items"][0]
    assert item["selection"] == {"kind": "default", "names": ()}
    assert [run["name"] for run in item["test_runs"]] == ["<default>@7"]


@pytest.mark.parametrize("limit", ["work-items", "test-runs"])
def test_planning_refuses_more_test_runs_than_a_campaign_holds(tmp_path: Path, limit: str):
    """The ceilings are planning's: every planner path reaches ``_work_items`` (#2136)."""
    if limit == "work-items":
        runs = tuple(build_test_run(f"t{index}", seed=1) for index in range(MAX_WORK_ITEMS + 1))
        with pytest.raises(
            TestRunLimitError, match=rf"plans 1001 work items.*at most {MAX_WORK_ITEMS} work items"
        ):
            _hdl_plan(tmp_path, tuple((run,) for run in runs))
    else:
        runs = tuple(build_test_run(f"t{index}", seed=1) for index in range(MAX_OBSERVATIONS + 1))
        with pytest.raises(
            TestRunLimitError, match=rf"and 4001 Test Runs.*{MAX_OBSERVATIONS} Test Runs.*fewer"
        ):
            _cocotb_plan(tmp_path, (runs,))


def test_hdl_work_item_refuses_more_than_one_test_run(tmp_path: Path):
    with pytest.raises(SimulationRecordIntegrityError, match="planned groups and Test Runs"):
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
    replanned = json.loads(encode_simulation_record_manifest(current))["work_items"]
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
    from booley.flows.sim.record import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from tests.flows.sim.simulation_record_test_support import _admission, _NoEdaExecutor

    return SimulationRecord(_NoEdaExecutor()).run(
        NewSimulationRecordRunRequest(
            plan=plan,
            project_root=tmp_path,
            report_root=None,
            policy=SimulationRecordPolicy(),
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
    # Name and fingerprint come from one authenticated document.
    assert [item["test_run_fingerprint"] for item in observations] == [
        run.fingerprint for run in runs
    ]


def test_unnamed_invocation_observation_is_named_by_its_test_run(tmp_path: Path):
    plan = _hdl_plan(tmp_path, ((build_test_run(None, seed=7),),), ())
    observations = _run_campaign(tmp_path, plan).acceptance_facts.document["observations"]
    assert [(item["test"], item["test_run"]) for item in observations] == [(None, "<default>@7")]


def test_tests_an_unfiltered_cocotb_batch_discovered_carry_its_seed():
    from booley.flows.sim.record.test_run_binding import observation_test_runs

    target = {"selector": "sim"}
    item = {"target": target, "test_runs": (test_run_document(build_test_run(None, seed=9)),)}
    observed = ({"test": "found_b"}, {"test": "found_a"})
    fingerprint = build_test_run(None).fingerprint
    assert observation_test_runs(item, observed) == (
        ("found_b@9", fingerprint),
        ("found_a@9", fingerprint),
    )
    unseeded = {"target": target, "test_runs": (test_run_document(build_test_run(None)),)}
    assert [name for name, _ in observation_test_runs(unseeded, observed)] == [
        "found_b",
        "found_a",
    ]


def test_an_observation_without_a_test_run_in_its_work_item_is_refused():
    from booley.flows.sim.record.test_run_binding import observation_test_runs

    item = {"target": {"selector": "sim"}, "test_runs": (test_run_document(_SOAK),)}
    with pytest.raises(SimulationRecordIntegrityError, match="has no Test Run"):
        observation_test_runs(item, ({"test": "reset"},))
    with pytest.raises(SimulationRecordIntegrityError, match="has no Test Run"):
        observation_test_runs(item, ({"test": "count"}, {"test": "count"}))


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
    # A Criterion names a Test: the Test with several Test Runs has no single
    # observation and is left out, so no run stands in for another.
    for indexer in (_candidate_simulation_observations, _candidate_cycle_observations):
        assert list(indexer(facts, outcome.target)) == ["reset"]


def test_acceptance_keeps_indexing_one_test_run_per_test_by_test(tmp_path: Path):
    from booley.flows.sim.acceptance import _candidate_simulation_observations

    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))
    indexed = _candidate_simulation_observations(outcome.acceptance_facts.document, outcome.target)
    assert list(indexed) == ["reset", "count"]


def _stored_result(tmp_path: Path):
    """Run a Cocotb batch campaign through the real result writer."""
    from booley.flows.sim.record import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.simulation_record_test_support import _admission, _plan
    from tests.flows.sim.test_simulation_record_characterization import _NoVerdictExecution

    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    execution = _NoVerdictExecution(eda_tool="verilator", cocotb=True)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None, execution_factory=lambda _options: execution
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    outcome = SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan, tmp_path, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
        )
    )
    store = SimulationRecordStore(invocation / "targets/sim/record")
    return outcome, [item.result for item in store.scan().items]


def test_written_results_are_v3_and_name_their_test_run(tmp_path: Path):
    outcome, results = _stored_result(tmp_path)
    assert [result.document["$schema"] for result in results] == ["booley.simulation-result/v4"]
    observations = results[0].document["observations"]
    assert [(item["test"], item["test_run"]) for item in observations] == [
        ("count", "count"),
        ("reset", "reset"),
    ]
    # Facts copy the name the result itself carries.
    assert [item["test_run"] for item in outcome.acceptance_facts.document["observations"]] == [
        "count",
        "reset",
    ]


def _result_document(tmp_path: Path) -> dict:
    from booley.flows.sim.record.codec import encode_simulation_result

    _outcome, results = _stored_result(tmp_path)
    return json.loads(encode_simulation_result(results[0]))


def test_result_v3_round_trips(tmp_path: Path):
    from booley.flows.sim.record.codec import (
        canonical_json_bytes,
        decode_simulation_result,
        encode_simulation_result,
    )

    document = _result_document(tmp_path)
    raw = canonical_json_bytes(document)
    assert encode_simulation_result(decode_simulation_result(raw)) == raw


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        (lambda document: document.update({"$schema": "booley.simulation-result/v5"}), "schema"),
    ],
)
def test_result_v3_refusals(tmp_path: Path, alter, message):
    from booley.flows.sim.record.codec import canonical_json_bytes, decode_simulation_result

    document = _result_document(tmp_path)
    alter(document)
    with pytest.raises(SimulationRecordIntegrityError, match=message):
        decode_simulation_result(canonical_json_bytes(document))
