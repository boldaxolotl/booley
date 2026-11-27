"""Crash-safe Simulation Record compatibility projections."""

import errno
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim import record_reports
from booley.flows.sim.coverage_record import CoverageRollup
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import collect_pre_sim_firings

# Private seam: the card text and the MCP detail are built only by these functions, and the
# public path (a full run) would need one run per hand-built observation set.
from booley.flows.sim.record_presentation import (
    _simulation_record_report_lines,
    _simulation_record_structured_details,
)
from booley.flows.sim.record_reports import simulation_record_invocation_lock
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import build_test_run

# Private seam: the MCP reader of report.json is the consumer these regressions pin; it has no
# public alias.
from booley.mcp.job_results import _read_report_json
from booley.runtime.file_lock import LockContentionError
from tests.conftest import symlink_or_skip
from tests.flows.sim.simulation_record_test_support import (
    _recorded_simulation_record,
    prerequisite_documents,
)
from tests.flows.sim.test_verilator_build_reuse_record import flow_project
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.runtime.test_sandbox_layout import project_alias
from tests.time_budget import hang_guard_s

__all__ = ["flow_project", "project_alias"]


@pytest.mark.parametrize("relative", [False, True])
def test_project_alias_simulation_executes_simulation_record(
    flow_project, tmp_path, monkeypatch, relative
):
    from booley.harness import doctor
    from tests.conftest import require_symlinks

    require_symlinks(tmp_path)
    data = tmp_path / "external-project-data"
    data.mkdir()
    alias = tmp_path / "booley"
    symlink_or_skip(alias, data, target_is_directory=True)
    monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", str(alias))
    from tests.runtime.test_sandbox_layout import configure_project_alias

    configure_project_alias(monkeypatch, alias, data)
    if relative:
        monkeypatch.chdir(tmp_path)
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=Path("booley/reports") if relative else alias / "reports",
            test=("first",),
            timeout_ms=hang_guard_s() * 1000,
        )
    )
    if result.exit_code:
        assert "Invocation lock paths must not contain symlinks" in result.outcome.report_text
    assert result.exit_code == 0, result.outcome.report_text
    assert list((data / "reports").glob("sim/*/targets/sim/record/manifest.json"))


def test_compatibility_projection_stays_incomplete_until_acceptance_commits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "simulation.json"
    report = {"target": "sim_uart", "complete": True, "tests_passed": 1}

    record_reports.write_compatibility_projection(path, report, acceptance_committed=False)
    assert json.loads(path.read_text())["complete"] is False

    record_reports.write_compatibility_projection(path, report, acceptance_committed=True)
    assert json.loads(path.read_text()) == report


def test_failed_projection_replacement_preserves_previous_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "simulation.json"
    record_reports.write_compatibility_projection(
        path, {"target": "sim_uart"}, acceptance_committed=False
    )
    before = path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError(f"injected replacement failure for {target}")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replacement failure"):
        record_reports.write_compatibility_projection(
            path, {"target": "sim_uart"}, acceptance_committed=True
        )

    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("budget", [80, 300, 4000, 12000])
def test_cycle_card_global_limits_and_utf8_names(tmp_path, monkeypatch, budget):

    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(budget * 2))
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(budget))
    run = build_test_run("é" * 256)
    recorded = _recorded_simulation_record(tmp_path, (run,))
    outcomes = [
        SimpleNamespace(
            target={"selector": str(index)},
            manifest_path=Path("/manifest.json"),
            aggregate_grade="fail",
            observations=[
                recorded.fact(run, test="é" * 256, cycle_count=value, detail={})
                for value in range(100)
            ],
        )
        for index in range(2)
    ]
    base = [
        SimpleNamespace(
            target=item.target,
            manifest_path=item.manifest_path,
            aggregate_grade=item.aggregate_grade,
            observations=[],
        )
        for item in outcomes
    ]
    plain = "\n".join(_simulation_record_report_lines(base))
    rendered = "\n".join(_simulation_record_report_lines(outcomes))
    added = len(rendered.encode()) - len(plain.encode())
    assert added <= min(budget // 4, max(0, budget - len(plain.encode()) - 2))
    assert rendered.count("cycles=") <= 32
    assert rendered.splitlines()[0] == plain.splitlines()[0]
    if "cycles=" in rendered:
        assert "…: cycles=" in rendered
    if added:
        assert "cycle counts omitted" in rendered


def test_simulation_record_card_renders_each_target_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "12000")
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "12000")
    smoke = build_test_run("smoke")
    recorded = _recorded_simulation_record(tmp_path, (smoke,))
    outcomes = [
        SimpleNamespace(
            target={"selector": name},
            manifest_path=Path("/manifest.json"),
            aggregate_grade="pass",
            observations=[recorded.fact(smoke, test="smoke", cycle_count=0, detail={})],
        )
        for name in ("one", "two")
    ]
    text = "\n".join(_simulation_record_report_lines(outcomes))
    assert text.count("smoke: cycles=0") == 2


@pytest.mark.parametrize("reason", ["x\n", "x\r\nz", "x\u2028z"])
def test_cycle_card_preserves_complete_reason_strings(reason, tmp_path):
    one, two = build_test_run("one"), build_test_run("two")
    recorded = _recorded_simulation_record(tmp_path, (one, two))
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=Path("/target/record/manifest.json"),
        aggregate_grade="fail",
        coverage_reference=object(),
        observations=[
            recorded.fact(one, test="one", cycle_count=7, detail={"reason": reason}),
            recorded.fact(two, test="two", cycle_count=None, detail={"reason": "y"}),
        ],
    )
    coverage_record = _coverage_record([], status="collector_error")
    rendered = _simulation_record_report_lines([outcome], {"sim": coverage_record})[0]
    assert f"\n  {reason}\n  y\n  one: cycles=7" in rendered


def test_large_persisted_count_mapping_survives_actual_readers(tmp_path, monkeypatch):
    # Private seam: `_simulation_record_endpoint_outcome` builds the outcome from 2,000 hand-
    # made observations per Target, which a real run cannot produce, and
    # `_structured_from_report` is the MCP reader the regression pins.
    from dataclasses import replace

    from booley.flows.endpoint_reporting import write_report
    from booley.mcp.server import _structured_from_report
    from tests.flows.sim.test_endpoint_record_lifecycle import _public_cycle_simulation_record

    simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    observations = tuple(
        dict(
            simulation_record.observations[0],
            test=None if i == 0 else '\\"é' * 100 + str(i),
            cycle_count=i,
        )
        for i in range(2000)
    )
    outcomes = [
        replace(simulation_record, target={"selector": name}, observations=observations)
        for name in ("one", "two")
    ]
    flow = SimulateFlow()
    flow._args = SimRequest(target="one,two", report_dir=tmp_path / "large", work_dir=tmp_path)
    result = flow._simulation_record_endpoint_outcome(outcomes)
    with flow.publication_resources:
        path = write_report(flow, result)
    report = _read_report_json(path)
    assert (
        json.loads(path.read_bytes())
        == report
        == json.loads((tmp_path / "large/sim.json").read_bytes())
    )
    assert sum(map(len, report["cycle_counts"].values())) == 4000
    assert report["cycle_counts"]["one"][0] == {"test": None, "cycle_count": 0}
    assert len(json.dumps(_structured_from_report(report)).encode()) < 65536
    assert "cycle_counts" not in report["detail"]


def _resume_hook_simulation_record(tmp_path, monkeypatch, interrupted, *, external=False):
    from booley.flows.sim.record.coordinator import (
        ResumeSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.model import create_simulation_record_plan
    from booley.flows.sim.record.planning import manifest_digest
    from booley.flows.sim.record.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.simulation_record_test_support import _admission, _executor, _handle
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    origin, original = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    store = SimulationRecordStore(original.manifest_path.parent)
    if interrupted:
        (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    manifest = store.load_manifest()
    node = ValidatedManifestNode(original.manifest_path, manifest, manifest_digest(manifest))
    project = tmp_path / "project"
    validated = ValidatedResumeManifest(node, (), (_handle(project),))
    invocation = tmp_path / "external/reports/1" if external else origin.parent / "2"
    invocation.mkdir(parents=True)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    resumed = SimulationRecord(_executor(tmp_path / "build", counters)).run(
        ResumeSimulationRecordRunRequest(
            validated,
            create_simulation_record_plan(manifest),
            project,
            origin.parent,
            SimulationRecordPolicy(),
            invocation,
            _admission(),
        )
    )
    return original, resumed, counters, invocation


@pytest.mark.timeout(120)  # the external resume case runs a second full Simulation Record
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_separates_historical_hooks_from_real_new_firings(
    tmp_path, monkeypatch, interrupted, external
):
    # Private seam: the endpoint outcome is built inside `SimulateFlow.run_prepared_simulation`;
    # this regression needs the hook counts of a record resumed directly from the coordinator.

    original, resumed, counters, invocation = _resume_hook_simulation_record(
        tmp_path, monkeypatch, interrupted, external=external
    )
    if external:  # an external invocation reuses the number of the producing one
        assert original.producer_invocation_directory.name == invocation.name == "1"
        assert original.producer_invocation_directory != invocation
    expected_runs = int(interrupted)
    assert counters["launch"] == expected_runs
    if not external:  # an external invocation reuses the durable build instead
        assert counters == {"compile": expected_runs, "durable_reuse": 0, "launch": expected_runs}
    assert len(resumed.pre_sim_firings) == 1 + expected_runs
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"])
    result = flow._simulation_record_endpoint_outcome([resumed])
    assert len(result.detail["pre_sim_lines"]) == expected_runs
    assert result.report_text.count("pre_run_commands") == expected_runs
    assert result.detail["pre_sim_current"] == expected_runs
    assert result.detail["pre_sim_total"] == 1 + expected_runs
    assert result.detail["pre_sim_historical"] == 1


def test_hook_reference_resolves_from_numbered_report_and_requires_external_context(
    tmp_path, monkeypatch
):
    # Private seam: the hook reference is built at publication for a destination directory the
    # public run cannot choose, so the test builds it for a numbered and an external
    # destination.
    from booley.flows.sim.record import ArtifactReferenceError, resolve_report_artifact_reference
    from booley.flows.sim.record_presentation import (
        _pre_sim_projection,
        _simulation_record_hook_inputs,
    )
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    invocation, outcome = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    detail = _pre_sim_projection(
        *_simulation_record_hook_inputs([outcome], invocation), invocation
    )[0]
    reference = detail["pre_sim_runs"][0]["reference"]
    owner = outcome.pre_sim_firings[0].document["attempt_id"]
    options = {
        "expected_kind": "pre_sim_commands",
        "expected_owner": owner,
        "maximum": 16 * 1024 * 1024,
    }
    resolved = resolve_report_artifact_reference(invocation / "report.json", reference, **options)
    assert resolved.path == outcome.pre_sim_firings[0].path
    destination = tmp_path / "external/reports/2"
    external = _pre_sim_projection(
        *_simulation_record_hook_inputs([outcome], destination), destination
    )[0]["pre_sim_runs"][0]["reference"]
    assert external["path_base"] == "external_origin_target"
    with pytest.raises(ArtifactReferenceError):
        resolve_report_artifact_reference(destination / "report.json", external, **options)
    resolved = resolve_report_artifact_reference(
        destination / "report.json",
        external,
        external_origin_target=outcome.manifest_path.parent.parent,
        **options,
    )
    assert resolved.path == outcome.pre_sim_firings[0].path


def _production_resume_flow(tmp_path, monkeypatch, original, invocation, counters):
    # Private seam: the resume needs a typed execution without destination admission or origin
    # locking, so the test replaces `_simulation_record_policy`,
    # `_lock_simulation_record_resume_origin`, the ordinary planning (real planning cannot
    # reproduce this synthetic manifest) and the ordinary executor.

    from booley.flows.sim import flow as sim_flow
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.flows.sim.record.coordinator import SimulationRecordPolicy
    from booley.flows.sim.record.model import create_simulation_record_plan
    from booley.flows.sim.record.planning import manifest_digest
    from booley.flows.sim.record.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.simulation_record_test_support import _executor, _handle

    manifest = SimulationRecordStore(original.manifest_path.parent).load_manifest()
    node = ValidatedManifestNode(original.manifest_path, manifest, manifest_digest(manifest))
    validated = ValidatedResumeManifest(node, (), (_handle(tmp_path / "project"),))
    invocation.mkdir(parents=True, exist_ok=True)
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(
        target=["sim"],
        work_dir=tmp_path / "project",
        report_dir=invocation.parent,
        dry_run=False,
        mode=None,
    )
    monkeypatch.setattr(flow, "_simulation_record_policy", SimulationRecordPolicy)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: invocation)
    # Destination admission and origin locking are outside this typed execution test.
    monkeypatch.setattr(flow, "_lock_simulation_record_resume_origin", lambda *args: None)
    planning = sim_flow.OrdinarySimulationRecordPlanning((SimpleNamespace(selector="sim"),), {})
    monkeypatch.setattr(flow, "_plan_ordinary_simulation_records", lambda *args: planning)
    monkeypatch.setattr(
        flow,
        "_ordinary_simulation_record_plan",
        lambda *args, **kwargs: create_simulation_record_plan(manifest),
    )
    build = sim_flow.OrdinaryHdlSerialExecutor
    # Each flow keeps its own counters: the executor is chosen by the flow that builds it.
    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor",
        lambda *, invoke: (
            _executor(invocation / "build", counters)
            if invoke.__self__ is flow
            else build(invoke=invoke)
        ),
    )
    return flow, PreparedSimulationEndpoint((), validated, ("sim",), {})


def _third_invocation_resume(tmp_path, monkeypatch, *, interrupted=False):
    """Resume an externally resumed Simulation Record again from a third invocation."""
    original, _prior, _counters, _invocation = _resume_hook_simulation_record(
        tmp_path, monkeypatch, False, external=True
    )
    if interrupted:
        (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    flow, prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "third/reports/1", counters
    )
    return original, flow, prepared, counters


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_resume_corruption_uses_simulation_record_integrity_outcome(
    tmp_path, monkeypatch, dry_run
):
    from tests.flows.sim.simulation_record_test_support import _admission

    original, flow, prepared, counters = _third_invocation_resume(tmp_path, monkeypatch)
    flow._args.dry_run = dry_run
    (original.pre_sim_firings[0].path.parents[3] / "result.json").write_text("{}")
    result = flow.run_prepared_simulation(prepared, _admission())
    assert result.exit_code == 2
    assert "Simulation Record integrity failure:" in result.report_text
    assert "Pre-Sim Commands evidence integrity failure" not in result.report_text
    assert counters["launch"] == 0


def _serialize_resume_waiter(monkeypatch, flow, waiting, completed):
    # Private seam: the concurrency test must block the waiter before store admission, and
    # `_publish_and_run_simulation_record_requests` is the only point before it.
    run = flow._publish_and_run_simulation_record_requests

    def wait_for_owner(*args, **kwargs):
        waiting.set()
        assert completed.wait(hang_guard_s())
        return run(*args, **kwargs)

    monkeypatch.setattr(flow, "_publish_and_run_simulation_record_requests", wait_for_owner)


def test_concurrent_production_resume_waiter_does_not_claim_owner_firing(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from tests.flows.sim.simulation_record_test_support import _admission

    original, _prior, _counters, _invocation = _resume_hook_simulation_record(
        tmp_path, monkeypatch, False, external=True
    )
    (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    first_counts = {"compile": 0, "durable_reuse": 0, "launch": 0}
    second_counts = dict(first_counts)
    owner, owner_prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "owner/reports/1", first_counts
    )
    waiter, waiter_prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "waiter/reports/1", second_counts
    )
    waiting, completed = Event(), Event()
    # Model serialization before store admission; the store itself rejects busy owners.
    _serialize_resume_waiter(monkeypatch, waiter, waiting, completed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiter.run_prepared_simulation, waiter_prepared, _admission())
        assert waiting.wait(hang_guard_s())
        try:
            owner_result = owner.run_prepared_simulation(owner_prepared, _admission())
        finally:
            completed.set()
        waiter_result = future.result(timeout=hang_guard_s())
    assert first_counts["launch"] == 1
    assert second_counts["launch"] == 0
    assert owner_result.detail["pre_sim_current"] == 1
    assert waiter_result.detail["pre_sim_current"] == 0
    assert waiter_result.detail["pre_sim_lines"] == []
    assert waiter_result.detail["pre_sim_historical"] == 2


def test_production_postscan_corruption_preserves_authenticated_current_evidence(
    tmp_path, monkeypatch
):
    # Private seam: the regression needs a corrupted record between the authenticated projection
    # and the endpoint, which the public run cannot pause at; the test wraps
    # `_simulation_record_endpoint_outcome`.
    from tests.flows.sim.simulation_record_test_support import _admission

    original, flow, prepared, counters = _third_invocation_resume(
        tmp_path, monkeypatch, interrupted=True
    )
    result_path = original.pre_sim_firings[0].path.parents[3] / "result.json"
    project = flow._simulation_record_endpoint_outcome

    def corrupt_after_authenticated_projection(outcomes):
        result = project(outcomes)
        assert result.detail["pre_sim_current"] == 1
        result_path.write_text("{}")
        return result

    monkeypatch.setattr(
        flow, "_simulation_record_endpoint_outcome", corrupt_after_authenticated_projection
    )
    result = flow.run_prepared_simulation(prepared, _admission())
    assert counters["launch"] == 1
    assert result.exit_code == 2
    assert result.detail["pre_sim_current"] == 1
    assert len(result.detail["pre_sim_lines"]) == 1
    assert result.report_text.count("pre_run_commands") == 1
    assert "Simulation Record integrity failure" in result.report_text
    assert "record_integrity_error" in result.detail


def _observed_hook_request(tmp_path, observer):
    from dataclasses import replace

    from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.simulation_record_test_support import _executor, _handle, _request
    from tests.flows.sim.test_endpoint_record_lifecycle import _pre_sim_plan

    (tmp_path / ".booley").mkdir(exist_ok=True)
    commands = ("echo observed-hook",)
    plan, _disclosures = _pre_sim_plan("immutable", commands, lambda document: None)
    store = SimulationRecordStore(tmp_path / "reports/1/targets/sim/record")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    request = replace(request, pre_sim_firing_published=observer)
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    evidence = run_pre_sim_commands(
        _handle(tmp_path),
        test_names=("alpha",),
        build_root=tmp_path / "build",
        eda_tool="icarus",
        timeout_s=hang_guard_s(),
        resource_flags=(),
        commands=commands,
        run_cwd=".",
    )
    return request, evidence


@pytest.mark.parametrize("stop", ["before", "after", "observer"])
def test_actual_publication_observer_checkpoint_and_exception_semantics(tmp_path, stop):
    from booley.flows.sim.record.pre_sim_evidence import publish_pre_sim_firing

    events = []

    def observer(key):
        events.append(key)
        if stop == "observer":
            raise RuntimeError("observer failure")

    def checkpoint(name):
        if name == f"{stop}:pre_sim_evidence":
            raise RuntimeError("checkpoint failure")

    request, evidence = _observed_hook_request(tmp_path, observer)
    with pytest.raises(RuntimeError, match="failure"):
        publish_pre_sim_firing(request, evidence, checkpoint=checkpoint)
    sidecars = tuple((request.attempt_directory / "pre-sim").glob("*.json"))
    assert len(events) == len(sidecars) == int(stop != "before")
    if sidecars:
        firings = collect_pre_sim_firings(request.store.manifest_path)
        assert [firing.key for firing in firings] == events
        assert firings[0].document["stdout_tail"] == "observed-hook\n"


def _current_observed_hook_dag(tmp_path, monkeypatch, flow):
    # Private seam: the observer that records each Pre-Sim firing a published run saw is
    # `_record_pre_sim_firing`; only the Flow's own observer proves its authority.
    from booley.flows.sim.record.model import create_simulation_record_plan
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record
    from tests.flows.sim.test_simulation_record_dependency import _add_observed_cycles

    _add_observed_cycles(monkeypatch)
    invocation = tmp_path / "reports/1"
    baseline_root, candidate_root = tmp_path / "baseline", tmp_path / "candidate"
    baseline_root.mkdir()
    candidate_root.mkdir()
    flow._reserved_invocation_dir = invocation
    flow._current_published_pre_sim_keys = set()

    def baseline_role(document):
        document["simulation_record_id"] = "01234567-89ab-4def-8123-456789abcdef"
        document["target"].update(role="cycle_count_baseline", revision="baseline")

    _, baseline = _successful_pre_sim_record(
        baseline_root,
        monkeypatch,
        "immutable",
        transform=baseline_role,
        invocation_directory=invocation,
        observer=flow._record_pre_sim_firing,
    )
    manifest = SimulationRecordStore(baseline.manifest_path.parent).load_manifest()
    prerequisites = prerequisite_documents(
        create_simulation_record_plan(manifest), baseline.manifest_path.relative_to(invocation)
    )
    _, candidate = _successful_pre_sim_record(
        candidate_root,
        monkeypatch,
        "immutable",
        transform=lambda document: document.update(prerequisites=prerequisites),
        invocation_directory=invocation,
        observer=flow._record_pre_sim_firing,
    )
    return candidate


@pytest.fixture
def observed_hook_flow(tmp_path, monkeypatch) -> tuple[SimulateFlow, Any]:
    """A Simulation Flow that published a candidate record and its Cycle Count baseline."""
    flow = SimulateFlow()
    flow._args = SimpleNamespace(target=["sim"])
    return flow, _current_observed_hook_dag(tmp_path, monkeypatch, flow)


def test_retained_current_baseline_uses_actual_flow_publication_authority(observed_hook_flow):
    # Private seam: `_simulation_record_endpoint_outcome` and `_attach_published_pre_sim` run
    # inside `run_prepared_simulation` after a real simulation; the regression corrupts a record
    # between them.
    flow, candidate = observed_hook_flow
    result = flow._simulation_record_endpoint_outcome([candidate])
    assert result.detail["pre_sim_current"] == 1
    firing = next(f for f in candidate.pre_sim_firings if f.document["role"] == "candidate")
    (firing.path.parents[3] / "result.json").write_text("{}")
    result = flow._attach_published_pre_sim(result)
    assert result.exit_code == 2
    assert result.detail["pre_sim_current"] == 2
    assert result.detail["pre_sim_historical"] == 0
    assert set(result.detail["pre_sim_roles"]) == {"candidate", "cycle_count_baseline"}
    assert len(result.detail["pre_sim_lines"]) == 2
    assert "Candidate:" in result.report_text and "Cycle Count baseline:" in result.report_text
    assert all(record["terminal"] is False for record in result.detail["pre_sim_runs"])


def test_published_hook_collection_scans_each_simulation_record_once(
    observed_hook_flow, monkeypatch
):
    # Private seam: `_attach_published_pre_sim` is the call that scans hook storage, and it runs
    # only inside a full run after the endpoint outcome.
    from collections import Counter

    from booley.flows.sim.record.store import SimulationRecordStore
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow, candidate = observed_hook_flow
    scans = Counter()
    scan = SimulationRecordStore.scan

    def counted(store, *args, **kwargs):
        scans[store.manifest_path] += 1
        return scan(store, *args, **kwargs)

    monkeypatch.setattr(SimulationRecordStore, "scan", counted)
    result = flow._attach_published_pre_sim(
        EndpointOutcome(report_text=""), candidate.manifest_path
    )
    assert result.detail["pre_sim_total"] == result.detail["pre_sim_current"] == 2
    assert set(result.detail["pre_sim_roles"]) == {"candidate", "cycle_count_baseline"}
    assert sorted(scans.values()) == [1, 1], "each Simulation Record is scanned once"


def _published_hook_budget_boundary(flow, candidate, roomy):
    # Private seam: the budget is measured between `_simulation_record_report_lines`,
    # `_cycle_display_line`, `_attach_published_pre_sim` and
    # `_simulation_record_endpoint_outcome`, which a public run calls in one step.
    from booley.flows.sim import record_presentation
    from booley.runtime.endpoint_execution import EndpointOutcome

    baseline = EndpointOutcome(
        report_text="\n".join(
            record_presentation._simulation_record_report_lines([candidate], {}, None)
        )
    )
    baseline = flow._attach_published_pre_sim(baseline)
    line = record_presentation._cycle_display_line(candidate.observations[0], 10000)
    allowance = len(line.encode()) + 1 + len(b"  1 cycle counts omitted") + 1
    budget = len(baseline.report_text.encode()) + 2 + (allowance if roomy else 0)
    assert len(baseline.report_text.encode()) + 2 <= budget
    assert not roomy or budget // 4 >= allowance
    if roomy:
        candidate_hooks = flow._simulation_record_endpoint_outcome([candidate]).detail[
            "pre_sim_lines"
        ]
        assert allowance < len("\n".join(candidate_hooks).encode()) + 1
    return {"baseline": baseline, "budget": budget, "candidate": candidate}


def _published_hook_budget_run(tmp_path, monkeypatch, *, roomy=False, corrupt=False):
    # Private seam: the test replaces `_ordinary_simulation_record` to publish its own hook DAG,
    # then reads `_simulation_record_endpoint_outcome` before the final report is attached.
    from booley.flows.sim import flow as module
    from booley.flows.sim import record

    flow = module.SimulateFlow()
    flow._args = SimRequest(target="sim", coverage=False, mode=None)
    evidence = {}

    def run(_prepared):
        candidate = _current_observed_hook_dag(tmp_path, monkeypatch, flow)
        evidence.update(_published_hook_budget_boundary(flow, candidate, roomy))
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(evidence["budget"]))
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(evidence["budget"]))
        reads = []
        reader = record.collect_pre_sim_firings

        def read(*paths, **kwargs):
            reads.append(paths)
            return reader(*paths, **kwargs)

        monkeypatch.setattr(record, "collect_pre_sim_firings", read)
        if corrupt:
            firing = next(
                f for f in candidate.pre_sim_firings if f.document["role"] == "candidate"
            )
            (firing.path.parents[3] / "result.json").write_text("{}")
        result = flow._simulation_record_endpoint_outcome([candidate])
        assert reads == []
        evidence["reads"] = reads
        evidence["endpoint"] = result
        evidence["endpoint_text"] = result.report_text
        return result

    monkeypatch.setattr(flow, "_ordinary_simulation_record", run)
    prepared = module.PreparedSimulationEndpoint((), None, ("sim",), {})
    result = flow.run_prepared_simulation(prepared, None)
    return flow, result, evidence


@pytest.mark.parametrize("roomy", [False, True])
def test_final_published_baseline_hooks_bound_cycle_text(tmp_path, monkeypatch, roomy):
    flow, result, evidence = _published_hook_budget_run(tmp_path, monkeypatch, roomy=roomy)
    assert result.detail["pre_sim_current"] == 2
    assert len(flow._current_published_pre_sim_keys) == 2
    assert len(result.detail["pre_sim_lines"]) == 2
    (paths,) = evidence["reads"]
    invocation = flow._reserved_invocation_dir
    assert set(paths) == set((invocation / "targets").glob("*/record/manifest.json"))
    assert result.report_text.count("pre_run_commands") == 2
    assert result.report_text.count("Candidate:") == 1
    assert result.report_text.count("Cycle Count baseline:") == 1
    size = len(result.report_text.encode())
    assert size + 2 <= evidence["budget"]
    assert result.report_text.count("cycles=17") == int(roomy)
    if not roomy:
        assert result.report_text == evidence["baseline"].report_text


def test_missing_published_hook_key_suppresses_optional_budget(observed_hook_flow):
    # Private seam: a published key that no record holds cannot be produced by a public run, so
    # the test calls `_simulation_record_endpoint_outcome` and reads
    # `_simulation_record_outcomes` directly.
    flow, candidate = observed_hook_flow
    flow._args = SimRequest(target="sim")
    flow._current_published_pre_sim_keys.add(("missing", "key", "id", 1))
    before = candidate.pre_sim_firings
    result = flow._simulation_record_endpoint_outcome([candidate])
    assert "cycles=" not in result.report_text and "integrity" not in result.report_text
    assert candidate.pre_sim_firings is before
    assert flow._simulation_record_outcomes == (candidate,)


@pytest.mark.parametrize("count", [None, 0, 1234])
def test_initialized_empty_hook_authority_keeps_authenticated_counts(tmp_path, monkeypatch, count):
    # Private seam: `_simulation_record_endpoint_outcome` is the call under test, with an empty
    # hook authority that a public run never leaves behind.
    from dataclasses import replace

    from booley.flows.sim import record
    from tests.flows.sim.test_endpoint_record_lifecycle import _public_cycle_simulation_record

    outcome = _public_cycle_simulation_record(tmp_path, monkeypatch)
    outcome = replace(outcome, observations=(dict(outcome.observations[0], cycle_count=count),))
    flow = SimulateFlow()
    flow._args = SimRequest(target="sim")
    flow._reserved_invocation_dir = outcome.producer_invocation_directory
    flow._current_published_pre_sim_keys = set()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("endpoint must not scan hook storage")

    monkeypatch.setattr(record, "collect_pre_sim_firings", unexpected_read)
    result = flow._simulation_record_endpoint_outcome([outcome])
    assert ("cycles=" in result.report_text) is (count is not None)
    assert "cycle counts omitted" not in result.report_text
    assert result.detail["pre_sim_current"] == 0


def test_corruption_before_endpoint_keeps_final_integrity_policy(tmp_path, monkeypatch):
    # Private seam: `_simulation_record_outcomes` shows the outcomes the integrity failure
    # discarded; a public run exposes only the final report text.
    flow, result, evidence = _published_hook_budget_run(
        tmp_path, monkeypatch, roomy=True, corrupt=True
    )
    assert "cycles=17" in evidence["endpoint_text"]
    assert "integrity" not in evidence["endpoint_text"]
    assert result.exit_code == 2
    assert "Simulation Record integrity failure:" in result.report_text
    assert flow._simulation_record_outcomes == ()
    assert result.detail["pre_sim_current"] == 2
    assert all(record["terminal"] is False for record in result.detail["pre_sim_runs"])


@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_count_budget_uses_only_actual_current_hook_keys(
    tmp_path, monkeypatch, interrupted
):
    # Private seam: `_simulation_record_report_lines` is wrapped to read the budget it is given
    # mid-run; the public report only shows the final text.
    from booley.flows.sim import flow as module
    from tests.flows.sim.simulation_record_test_support import _admission
    from tests.flows.sim.test_simulation_record_dependency import _add_observed_cycles

    _add_observed_cycles(monkeypatch)
    original, flow, prepared, counters = _third_invocation_resume(
        tmp_path, monkeypatch, interrupted=interrupted
    )
    probe = module._simulation_record_report_lines
    calls = []

    def inspect(outcomes, coverage_records, trailing_lines, **kwargs):
        assert outcomes[0].manifest_path == original.manifest_path
        calls.append((frozenset(flow._current_published_pre_sim_keys), trailing_lines))
        return probe(outcomes, coverage_records, trailing_lines, **kwargs)

    monkeypatch.setattr(module, "_simulation_record_report_lines", inspect)
    result = flow.run_prepared_simulation(prepared, _admission())
    assert counters["launch"] == int(interrupted)
    assert result.report_text.count("cycles=17") == 1
    assert result.detail["pre_sim_current"] == int(interrupted)
    assert result.detail["pre_sim_historical"] == 1
    assert len(result.detail["pre_sim_lines"]) == int(interrupted)
    assert len(calls) == 1
    keys, lines = calls[0]
    assert keys == frozenset(flow._current_published_pre_sim_keys)
    assert len(keys) == len(lines) == int(interrupted)
    assert result.report_text.count("pre_run_commands") == int(interrupted)


def test_prior_firing_reauthentication_rejects_byte_identical_symlink_attempt(
    tmp_path, monkeypatch
):
    from booley.flows.sim.record import SimulationRecordIntegrityError
    from tests.conftest import require_symlinks
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    require_symlinks(tmp_path)
    _invocation, simulation_record = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    firing = simulation_record.pre_sim_firings[0]
    directory = firing.path.parent.parent
    copy = tmp_path / "copied-attempt"
    sidecar_bytes = firing.path.read_bytes()
    # An attempt directory holds no executable: the record keeps one, read-only,
    # in its shared bundle.
    assert not list(directory.rglob("simv"))
    executables = list(directory.parents[3].rglob("simv"))
    assert len(executables) == 1, "successful Simulation Record must retain one real executable"
    assert not executables[0].stat().st_mode & 0o222
    directory.rename(copy)
    assert (copy / "pre-sim/0001.json").read_bytes() == sidecar_bytes
    symlink_or_skip(directory, copy, target_is_directory=True)
    assert firing.path.read_bytes() == sidecar_bytes
    with pytest.raises(SimulationRecordIntegrityError):
        collect_pre_sim_firings(prior=(firing,))


@pytest.mark.parametrize("defect", ["root", "descendant", "lock", "target_link"])
def test_alias_keeps_user_report_links_strict(flow_project, project_alias, defect):

    alias, data = project_alias
    reports = data / "reports"
    reports.mkdir()
    if defect == "root":
        user = data / "user"
        user.symlink_to(reports, target_is_directory=True)
        report_dir = user
    elif defect == "target_link":
        (data / "linked").symlink_to(data, target_is_directory=True)
        report_dir = alias / "linked/reports"
    else:
        (reports / "sim").mkdir()
        if defect == "descendant":
            (reports / "sim").rmdir()
            (reports / "sim").symlink_to(data, target_is_directory=True)
        else:
            (reports / "sim/.invocation-1.lock").symlink_to(data / "outside")
        report_dir = alias / "reports"
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=report_dir,
            test=("first",),
            timeout_ms=hang_guard_s() * 1000,
        )
    )
    assert result.exit_code == 2
    assert "Invocation lock paths must not contain symlinks" in result.outcome.report_text
    assert not list(reports.rglob("manifest.json"))


@pytest.mark.parametrize("selection", ["default", "dry_run", "elab"])
def test_alias_normalization_precedes_simulation_preparation(
    flow_project, project_alias, monkeypatch, selection
):
    from booley.flows.sim.mode import SimulationMode

    alias, data = project_alias
    monkeypatch.setattr(
        "booley.runtime.booley_dir.resolve_checkout_booley_dir", lambda _root: alias
    )
    flow = SimulateFlow()
    result = flow.execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=None if selection == "default" else alias / "reports",
            dry_run=selection == "dry_run",
            mode=SimulationMode.ELAB_ONLY if selection == "elab" else None,
            test=None if selection == "elab" else ("first",),
            timeout_ms=hang_guard_s() * 1000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert flow.args.report_dir == data / ("flow-reports" if selection == "default" else "reports")


@pytest.mark.parametrize("error", ["schema", "missing"])
def test_projection_rejects_corrupt_record(tmp_path, error):
    from booley.flows.sim.record import decode_simulation_projection
    from tests.flows.sim.test_test_run_evidence_rows import _v3_document

    document = _v3_document(tmp_path)
    decode_simulation_projection(document, coverage_expected=False)
    if error == "schema":
        document["$schema"] = "unknown"
    else:
        del document["complete"]
    with pytest.raises(ValueError):
        decode_simulation_projection(document, coverage_expected=False)


def _two_firing_store(tmp_path):
    """Run a coverage Simulation Record whose one attempt holds two failed Pre-Sim firings."""
    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.verilator_coverage import SimulationRunResult
    from tests.flows.sim.simulation_record_test_support import _run_coverage_simulation_record
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    class Hooks(NativeExecution):
        def run(self, request):
            evidence = PreSimEvidence(
                ("prepare-vectors",),
                (request.test.name,),
                "failed",
                0.2,
                "vector generator rejected input",
                returncode=1,
            )
            return SimulationRunResult("elab_error", evidence.detail, evidence)

    outcome, store = _run_coverage_simulation_record(
        tmp_path, Hooks(), pre_sim_commands=("prepare-vectors",)
    )
    firings = collect_pre_sim_firings(store.manifest_path)
    assert len(firings) == 2 and outcome.complete
    return firings


def test_prior_firing_reauthentication_reads_each_attempt_once(tmp_path, monkeypatch):
    """A re-read returns only the prior firings it was given, decoding each sidecar once."""
    from tests.flows.sim.simulation_record_test_support import _observe_pre_sim_reads

    firings = _two_firing_store(tmp_path)
    first, second = firings
    assert first.path.parent == second.path.parent
    fresh = collect_pre_sim_firings(prior=(first,))
    assert [(f.key, f.path, f.terminal) for f in fresh] == [(first.key, first.path, False)]

    accesses, decodes, _results = _observe_pre_sim_reads(monkeypatch)
    collect_pre_sim_firings(prior=firings)
    assert decodes == {(f.key[2], f.key[3]): 1 for f in firings}, (
        "each sidecar must be decoded once"
    )
    assert accesses == {f.path: 1 for f in firings}, "each sidecar must have one byte access"


def test_shared_collection_bound_counts_distinct_prerequisite_references(
    observed_hook_flow, monkeypatch
):
    # Private seam: `_PRE_SIM_DAG_BOUND` is 4,096 roots, not buildable in a unit test, so the
    # test lowers it.
    from booley.flows.sim.record import SimulationRecordIntegrityError, store

    _flow, candidate = observed_hook_flow
    invocation = candidate.manifest_path.parents[3]
    # Three roots reach one baseline reference: only distinct references count.
    monkeypatch.setattr(store, "_PRE_SIM_DAG_BOUND", 1)
    roots = (
        *sorted((invocation / "targets").glob("*/record/manifest.json")),
        candidate.manifest_path,
    )
    assert len(collect_pre_sim_firings(*roots)) == 2
    monkeypatch.setattr(store, "_PRE_SIM_DAG_BOUND", 0)
    with pytest.raises(SimulationRecordIntegrityError, match="exceeds DAG bound"):
        collect_pre_sim_firings(*roots)


@pytest.mark.parametrize("linked", ["entry", "below", "above"])
def test_invocation_lock_follows_only_the_booley_dir_entry(tmp_path, linked):
    """#2191: the lock crosses the Booley Directory link, never a link below or above it."""
    project = tmp_path / "project"
    project.mkdir()
    entry = project / ".booley"
    symlinked_booley_dir(project, tmp_path / "external")
    if linked == "below":
        # A link below the entry (flow-reports) stays refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink_or_skip(entry / "flow-reports", outside, target_is_directory=True)
    invocation = entry / "flow-reports" / "sim" / "1"
    invocation.parent.mkdir(parents=True, exist_ok=True)
    if linked == "above":
        # The entry itself is exempt, but a link above it (the Project reached by alias) is not.
        alias = tmp_path / "alias"
        symlink_or_skip(alias, project, target_is_directory=True)
        entry = alias / ".booley"
        invocation = entry / "flow-reports" / "sim" / "1"

    if linked != "entry":
        with (
            pytest.raises(ValueError, match="must not contain symlinks"),
            record_reports.simulation_record_invocation_lock(invocation, booley_dir=entry),
        ):
            pass
        return
    with record_reports.simulation_record_invocation_lock(invocation, booley_dir=entry):
        assert (invocation.parent / ".invocation-1.lock").exists()


def test_invocation_booley_dir_ignores_a_refused_layout_beside_a_custom_root(
    tmp_path: Path,
) -> None:
    """#2191: probing a custom report root's parent never raises a layout error."""
    from booley.flows.sim.record_reports import invocation_booley_dir

    project = tmp_path / "project"
    (project / "out" / "flow-reports" / "sim" / "1").mkdir(parents=True)
    # A regular file where the Booley Directory belongs is a layout the resolver refuses.
    (project / ".booley").write_text("", encoding="utf-8")

    assert invocation_booley_dir(project / "out" / "flow-reports" / "sim" / "1") is None


@pytest.mark.parametrize(
    ("reports_name", "expect_refused"),
    [
        ("flow-reports", False),
        (".runtime/flow-reports", False),
        ("tmp/doctor/flow-reports", False),
        ("job_logs/session-1/.runtime/flow-reports", False),
        ("goals/demo-20261027T000000Z/.runtime/flow-reports", False),
        ("custom", True),
        ("tmp/flow-reports", True),
        ("job_logs/flow-reports", True),
        ("job_logs/a/b/.runtime/flow-reports", True),
        ("goals/flow-reports", True),
        ("other/run-1/.runtime/flow-reports", True),
    ],
)
def test_host_run_reserves_through_the_entry_only_for_a_derivable_report_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reports_name: str, expect_refused: bool
) -> None:
    """#2191: a custom report root under a linked entry is refused at the lock, not later.

    Booley-owned roots are derivable: the default ones, deep Doctor's own
    ``tmp/doctor/flow-reports``, and the per-run runtime roots of in-Sandbox MCP sessions
    MCP sessions (``job_logs/<session>/.runtime/flow-reports``) and Goal
    Records (``goals/<goal-id>/.runtime/flow-reports``). A Codespace links
    ``/work/.booley``.
    """
    from booley.runtime.booley_dir import reset_cache

    project = tmp_path / "project"
    project.mkdir()
    symlinked_booley_dir(project, tmp_path / "external")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.chdir(project)
    reset_cache()
    report_dir = project / ".booley" / reports_name

    flow = SimulateFlow()
    flow._args = SimRequest(report_dir=report_dir)
    if expect_refused:
        with pytest.raises(ValueError, match="must not contain symlinks"):
            flow.reserve_invocation_dir()
        return
    with flow.publication_resources:
        assert flow.reserve_invocation_dir() == report_dir / "sim" / "1"


_BUILD_STAGE = {
    "failure_kind": "infrastructure",
    "timed_out": True,
    "returncode": -9,
    "reason": "build exceeded timeout",
}


def _mixed_axes(index: int) -> dict[str, Any]:
    """Observations that differ on every axis; the last one timed out."""
    return {
        "test": f"test_{index:02d}",
        "execution": "timeout" if index == 39 else "completed",
        "failure_class": "design" if index == 39 else None,
        "functional": "fail" if index % 2 else "pass",
        "assertions": "dirty" if index % 3 == 0 else "clean",
        "assertion_count": index,
        "cycle_count": None,  # every real observation carries one; the metrics read it
        "detail": {"reason": f"detail-{index}"},
    }


_CLEAN_PASS: dict[str, Any] = {
    "execution": "completed",
    "failure_class": None,
    "functional": "pass",
    "assertions": "clean",
    "assertion_count": 0,
    "cycle_count": None,
    "detail": {},
}


def _passing_then(last: dict[str, Any]) -> Callable[[int], dict[str, Any]]:
    """32 clean passing observations, then the 33rd, which is past the preview."""
    return lambda index: {**_CLEAN_PASS, **last} if index == 32 else _CLEAN_PASS


def _check_mixed_axes(details: dict[str, Any]) -> None:
    assert details["observation_total"] == 40
    assert len(details["observations"]) == 32
    assert set(details["observations"][0]) == {
        "test",
        "execution",
        "failure_class",
        "functional",
        "assertions",
        "assertion_count",
        "cycle_count",
        "detail",
        "log",
        "trace",
    }
    assert details["observation_counts"]["execution"] == {"completed": 39, "timeout": 1}


def _check_infrastructure_termination(details: dict[str, Any]) -> None:
    assert details["termination_counts"] == {"disk_budget": 1}
    assert details["observations"][0]["failure_class"] is None


def _check_build_diagnostics(details: dict[str, Any]) -> None:
    assert details["build_stage"] == [_BUILD_STAGE]


def _check_cycle_counts(details: dict[str, Any]) -> None:
    assert details["observations"][-1]["cycle_count"] == 31
    assert "cycle_counts" not in details


_INFRASTRUCTURE = {
    "execution": "aborted",
    "failure_class": "infrastructure",
    "functional": "not_observed",
    "assertions": "not_observed",
}


@pytest.mark.parametrize(
    ("count", "fields", "check"),
    [
        (40, _mixed_axes, _check_mixed_axes),
        (
            33,
            _passing_then(
                {
                    **_INFRASTRUCTURE,
                    "detail": {"termination": "disk_budget", "reason": "disk guard"},
                }
            ),
            _check_infrastructure_termination,
        ),
        (
            33,
            _passing_then(
                {
                    **_INFRASTRUCTURE,
                    "detail": {"termination": "build_infrastructure", "build_stage": _BUILD_STAGE},
                }
            ),
            _check_build_diagnostics,
        ),
        (40, lambda index: {**_CLEAN_PASS, "cycle_count": index}, _check_cycle_counts),
    ],
    ids=["mixed-axes", "infrastructure-termination", "build-diagnostics", "cycle-counts"],
)
def test_mcp_simulation_record_details_bound_observations_without_collapsing_axes(
    tmp_path: Path, count: int, fields: Callable[[int], dict[str, Any]], check: Callable[..., None]
) -> None:
    runs = [build_test_run(f"test_{index:02d}") for index in range(count)]
    recorded = _recorded_simulation_record(tmp_path, runs)
    observations = tuple(
        recorded.fact(run, **{"test": f"test_{index:02d}", **fields(index)})
        for index, run in enumerate(runs)
    )
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=tmp_path / "targets/sim/record/manifest.json",
        summary_path=tmp_path / "targets/sim/record/summary.json",
        coverage_reference=None,
        aggregate_grade="fail",
        complete=True,
        observations=observations,
    )

    # Private seam: the 32-observation preview is the MCP detail of a record; only a full
    # run's report.json reaches it, and each case needs 33 to 40 hand-built observations.
    details = _simulation_record_structured_details((outcome,))["sim"]

    assert details["observation_total"] == count
    assert details["observations_truncated"] is True
    check(details)


def test_simulation_record_report_preserves_the_failed_simulator_reason(tmp_path: Path) -> None:
    unnamed = build_test_run(None)
    recorded = _recorded_simulation_record(tmp_path, (unnamed,), ())
    outcome = SimpleNamespace(
        target={"selector": "sim_fail"},
        manifest_path=tmp_path / "record/manifest.json",
        aggregate_grade="fail",
        observations=(
            recorded.fact(unnamed, test=None, detail={"reason": "intentional simulator failure"}),
        ),
    )

    assert _simulation_record_report_lines((outcome,), report_path=tmp_path / "sim.json")[0] == (
        "sim_fail: FAIL (Simulation Record unavailable)\n"
        f"  manifest: {(tmp_path / 'record/manifest.json').resolve()}\n"
        f"  report: {(tmp_path / 'sim.json').resolve()}\n"
        "  intentional simulator failure"
    )


@pytest.fixture
def reservation_flow(tmp_path: Path) -> tuple[SimulateFlow, Path]:
    """A Simulation Flow reserving numbers under ``tmp_path / "reports"``, and that root."""
    reports = tmp_path / "reports"
    flow = SimulateFlow()
    flow._args = SimRequest(report_dir=reports)
    return flow, reports


def test_simulation_reservation_locks_before_directory_publication(
    reservation_flow: tuple[SimulateFlow, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, reports = reservation_flow
    mkdir = type(reports).mkdir
    observed = False

    def inspect_before_publish(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal observed
        if path == reports / "sim/1":
            with (
                pytest.raises(LockContentionError),
                simulation_record_invocation_lock(reports / "sim/1"),
            ):
                raise AssertionError("the producer lock must be held")
            observed = True
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(reports), "mkdir", inspect_before_publish)
    with flow.publication_resources:
        assert flow.reserve_invocation_dir() == reports / "sim/1"

    assert observed


def test_simulation_reservation_skips_contended_and_raced_numbers(
    reservation_flow: tuple[SimulateFlow, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, reports = reservation_flow
    (reports / "sim").mkdir(parents=True)
    mkdir = type(reports).mkdir

    def lose_race_for_two(path: Path, *args: object, **kwargs: object) -> None:
        if path == reports / "sim/2":
            mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(reports), "mkdir", lose_race_for_two)
    with simulation_record_invocation_lock(reports / "sim/1"), flow.publication_resources:
        assert flow.reserve_invocation_dir() == reports / "sim/3"
        # The raced number's producer lock was released, not leaked.
        with simulation_record_invocation_lock(reports / "sim/2"):
            pass


def test_simulation_reservation_releases_its_lock_when_publication_fails(
    reservation_flow: tuple[SimulateFlow, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-race mkdir failure (EACCES, ENOSPC) must not leave the number busy."""
    flow, reports = reservation_flow
    (reports / "sim").mkdir(parents=True)
    mkdir = type(reports).mkdir

    def disk_full_for_one(path: Path, *args: object, **kwargs: object) -> None:
        if path == reports / "sim/1":
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(reports), "mkdir", disk_full_for_one)
    with (
        flow.publication_resources,
        pytest.raises(OSError, match="No space left") as raised,
    ):
        flow.reserve_invocation_dir()

    # The retained traceback keeps the reservation frame alive, so only an
    # explicit release (not refcount or GC finalization) frees the number.
    assert raised.tb is not None
    with simulation_record_invocation_lock(reports / "sim/1"):
        pass


def test_simulation_reservation_stops_at_the_invocation_ceiling(
    reservation_flow: tuple[SimulateFlow, Path],
) -> None:
    flow, reports = reservation_flow
    (reports / "sim" / "1000000").mkdir(parents=True)

    with (
        flow.publication_resources,
        pytest.raises(RuntimeError, match="reservation ceiling exceeded"),
    ):
        flow.reserve_invocation_dir()


def _rollup(metric: str, covered: int, eligible: int) -> CoverageRollup:
    """A stored rollup; ``percent`` follows the stored two-decimal rounding."""
    percent = None if eligible == 0 else round(covered * 100 / eligible, 2)
    return CoverageRollup(
        metric=metric,
        semantics="booley.coverage-semantics/v1",
        total_points=eligible,
        eligible_points=eligible,
        covered_points=covered,
        waived_points=0,
        percent=percent,
    )


def _coverage_record(
    rollups: list[CoverageRollup],
    thresholds: dict[str, float | None] | None = None,
    *,
    status: str = "complete",
    evaluation: str = "not_requested",
) -> SimpleNamespace:
    """A stub Coverage Record whose collection and evaluation ended as given."""
    return SimpleNamespace(
        collection={"status": status, "diagnostics": ()},
        evaluation={
            "status": evaluation,
            "diagnostics": (),
            "metrics": (),
            "thresholds": thresholds or {},
        },
        rollups=tuple(rollups),
        source_closure={},
        findings=(),
    )


def _coverage_card(tmp_path: Path, coverage_record: SimpleNamespace) -> list[str]:
    """Render one coverage Target's card block from *coverage_record*."""
    outcome = SimpleNamespace(
        target={"selector": "sim_soc"},
        manifest_path=tmp_path / "targets/sim_soc/record/manifest.json",
        aggregate_grade="pass",
        coverage_reference=object(),
        observations=(),
    )
    return _simulation_record_report_lines(
        (outcome,),
        {"sim_soc": coverage_record},
        trailing_lines=None,
        report_path=tmp_path / "r.json",
    )[0].splitlines()


@pytest.mark.parametrize(
    ("rollups", "thresholds", "expected"),
    [
        (
            [
                _rollup("line", 9239, 10000),
                _rollup("branch", 81, 100),
                _rollup("toggle", 642, 1000),
            ],
            None,
            "  coverage: line 92.3% · branch 81.0% · toggle 64.2%",
        ),
        (
            [
                _rollup("line", 7999, 10000),
                _rollup("branch", 81, 100),
                _rollup("expression", 8526, 10000),
                _rollup("toggle", 8524, 10000),
            ],
            {"line": 80, "branch": 80, "expression": 85.25, "toggle": 85.25},
            "  coverage: "
            + " · ".join(
                [
                    "line 79.9% (min 80%, missed)",
                    "branch 81.0% (min 80%, met)",
                    "expression 85.26% (min 85.25%, met)",
                    "toggle 85.24% (min 85.25%, missed)",
                ]
            ),
        ),
        # 1/3 is below the float 100/3; a float division would print it equal to the minimum.
        (
            [_rollup("line", 1, 3)],
            {"line": 100 / 3},
            "  coverage: line 33.333333333333333% (min 33.333333333333336%, missed)",
        ),
        (
            [_rollup("line", 1, 2)],
            {"line": 200 / 3},
            "  coverage: line 50.00000000000000% (min 66.66666666666667%, missed)",
        ),
        (
            [_rollup("line", 1, 2)],
            {"line": 1e-5},
            "  coverage: line 50.00000% (min 0.00001%, met)",
        ),
        (
            [_rollup("line", 1, 2), _rollup("toggle", 0, 0)],
            {"toggle": 80},
            "  coverage: line 50.0% · toggle n/a (min 80%, missed)",
        ),
        ([_rollup("toggle", 0, 0)], None, "  coverage: toggle n/a"),
    ],
    ids=[
        "every-metric",
        "thresholds-met-and-missed",
        "long-minimum-never-rounds-up",
        "repeating-minimum",
        "tiny-minimum-not-scientific",
        "na-with-minimum",
        "na-alone",
    ],
)
def test_coverage_card_metric_line(
    tmp_path: Path,
    rollups: list[CoverageRollup],
    thresholds: dict[str, float | None] | None,
    expected: str,
) -> None:
    lines = _coverage_card(tmp_path, _coverage_record(rollups, thresholds))

    assert lines[-2:] == [
        expected,
        f"  Coverage Record: {(tmp_path / 'targets/sim_soc/coverage.json').resolve()}",
    ]


def test_coverage_card_omits_metrics_unless_collection_complete(tmp_path: Path) -> None:
    lines = _coverage_card(
        tmp_path, _coverage_record([_rollup("line", 1, 2)], status="collector_error")
    )
    assert not any(line.startswith("  coverage: ") for line in lines)
    assert f"  Coverage Record: {(tmp_path / 'targets/sim_soc/coverage.json').resolve()}" in lines

    # A complete collection with no points at all has nothing to list either.
    lines = _coverage_card(tmp_path, _coverage_record([]))
    assert not any(line.startswith("  coverage: ") for line in lines)


@pytest.mark.parametrize(
    ("collection", "evaluation", "expected"),
    [
        (
            "collector_error",
            "not_requested",
            "coverage collection COLLECTOR_ERROR · evaluation NOT_REQUESTED",
        ),
        ("complete", "fail", "coverage collection COMPLETE · evaluation FAIL"),
    ],
)
def test_coverage_report_keeps_nonpassing_status_without_optional_detail(
    tmp_path: Path, collection: str, evaluation: str, expected: str
) -> None:
    lines = _coverage_card(
        tmp_path, _coverage_record([], status=collection, evaluation=evaluation)
    )

    assert lines[0].startswith(f"sim_soc: simulation PASS · {expected} (Simulation Record ")


def test_new_invocation_number_is_above_every_pruned_marker(
    reservation_flow: tuple[SimulateFlow, Path],
):
    flow, reports = reservation_flow
    sim = reports / "sim"
    for name in ("1", ".pruned-2", ".pruned-7"):
        (sim / name).mkdir(parents=True)
    with flow.publication_resources:
        assert flow.reserve_invocation_dir() == sim / "8"
