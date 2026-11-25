"""Execution, Pre-Sim Commands, run directories and recovery scans of Simulation Records."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.base import DEFAULT_TIMEOUT_S
from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.execution.contract import (
    PreSimEvidence,
    PreSimScopeStoppedError,
    SimulationOptions,
)
from booley.flows.sim.record import collect_pre_sim_firings, serial_execution
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.codec import (
    RECORD_MAX_BYTES,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
    WorkExecutionRequest,
)
from booley.flows.sim.record.coverage_execution import _CoverageAggregateError
from booley.flows.sim.record.model import (
    SimulationAttempt,
    SimulationResult,
    create_simulation_record_plan,
)
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.run_directory import (
    RunDirectory,
    claimed_run_directory,
    cleanup_interrupted_run_directory,
)
from booley.flows.sim.record.scheduler import BoundedSimulationRecordScheduler
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.verilator_coverage import SimulationRunResult
from booley.runtime.execution_records import (
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import CLASS_MEMORY, ClaimAbortedError, ClaimLostError, SlotStore
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    supervised_execution_scope,
)
from tests.conftest import symlink_or_skip
from tests.execution_test_support import run_with_recovery_between_read_and_write
from tests.flows.sim.simulation_record_test_support import (
    _BAD_DIGEST,
    _PARALLEL_SYNC_TIMEOUT_S,
    _admission,
    _completed,
    _Execution,
    _executor,
    _handle,
    _hook_publication_request,
    _item_capacity,
    _legacy_disclosures,
    _managed,
    _manifest_for,
    _NoEdaExecutor,
    _outer_claim,
    _own_attempt_run_directory,
    _publish_request_hook,
    _read,
    _request,
    _run_coverage_simulation_record,
    _run_identity,
    _scheduled_attempt,
    _two_item_manifest,
    _wait_until,
    _write,
)
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.flows.sim.test_endpoint_record_lifecycle import _pre_sim_plan
from tests.fusesoc_test_support import symlinked_booley_dir


def _project_with_build(tmp_path: Path) -> tuple[Path, Path]:
    """Return an empty project root and an engine build root holding one image."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    return project, build_root


def _published_request(tmp_path: Path, manifest: Any, **request_options: Any) -> tuple[Any, ...]:
    """Publish ``manifest`` and return its store, project, build root and first work request.

    A project directory that already exists under ``tmp_path`` is reused.
    """
    project, build_root = _project_with_build(tmp_path)
    store = SimulationRecordStore(tmp_path / "record")
    store.publish_manifest(manifest)
    request = _request(
        store,
        manifest,
        manifest.document["work_items"][0],
        project,
        invocation=1,
        **request_options,
    )
    return store, project, build_root, request


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_simulation_record_policy_carries_build_budget_into_execution_options(
    tmp_path: Path, access: str
) -> None:
    disclosures = {"alpha": _legacy_disclosures()["alpha"]} if access == "legacy-per-test" else {}
    manifest = _manifest_for(("alpha",), access=access)
    if disclosures:
        document = json.loads(canonical_json_bytes(manifest.document))
        document.pop("fingerprints")
        document["planning_disclosures"] = list(disclosures.values())
        manifest = finalize_manifest(document)
    _store, _project, build_root, request = _published_request(
        tmp_path,
        manifest,
        policy=SimulationRecordPolicy(timeout_seconds=5, build_timeout_seconds=7),
    )
    captured: list[SimulationOptions] = []
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    def factory(options: SimulationOptions) -> _Execution:
        captured.append(options)
        return _Execution(build_root, counters, disclosures=disclosures)

    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=factory,  # type: ignore[arg-type,return-value]
    )
    executor.execute(request)

    assert captured == [SimulationOptions(timeout_ms=5000, build_timeout_ms=7000)]
    assert counters["compile"] == 1


def test_pre_cancelled_simulation_record_hook_never_compiles_or_runs(tmp_path):
    plan, _disclosures = _pre_sim_plan("immutable", ("echo actual-hook",), lambda _document: None)
    _store, _project, build_root, request = _published_request(tmp_path, plan.manifest)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    executor = _executor(build_root, counters)
    scope = SupervisedExecutionScope(None, tmp_path / ".booley", lambda: True)
    with supervised_execution_scope(scope), pytest.raises(PreSimScopeStoppedError):
        executor.execute(request)
    assert counters == {"compile": 0, "durable_reuse": 0, "launch": 0}
    assert list(request.attempt_directory.glob("pre-sim/*.json")) == []


def _packaged_source_disclosure(raw: bytes) -> dict[str, object]:
    return {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [
            {
                "path": "booley-package/refs/booley_vcd_dump.sv",
                "bytes": len(raw),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "kind": "generated_input",
            }
        ],
        "tool_provenance": {"kind": "fusesoc", "version": "2.4.6", "contract_version": "1"},
        "cleanup": {"removed": True},
    }


@pytest.mark.parametrize("mapped", [False, True])
def test_changed_packaged_source_fails_disclosure_equality_before_compile(
    tmp_path: Path, mapped: bool
) -> None:
    planned = _packaged_source_disclosure(b"planned wheel bytes")
    execution = _packaged_source_disclosure(b"different execution wheel bytes")
    if mapped:
        execution = _packaged_source_disclosure(b"planned wheel bytes")
        planned["generated_files"][0].update(path="staged.hex", source_path="data/source.hex")
        execution["generated_files"][0].update(path="staged.hex", source_path="other/source.hex")
    base_manifest = _manifest_for(("alpha",))
    document = json.loads(canonical_json_bytes(base_manifest.document))
    document.pop("fingerprints")
    document["planning_disclosures"] = [planned]
    manifest = finalize_manifest(document)
    _store, _project, build_root, request = _published_request(tmp_path, manifest)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    with pytest.raises(
        SimulationRecordIntegrityError,
        match="prepared generator source closure disagrees with record plan",
    ):
        _executor(build_root, counters, disclosures={"alpha": execution}).execute(request)

    assert counters["compile"] == 0


def test_immutable_hook_compile_surface_mutation_stops_before_snapshot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_for(("alpha",))
    plan = create_simulation_record_plan(manifest)
    project, build_root = _project_with_build(tmp_path)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    surfaces = iter(("before", "after"))
    monkeypatch.setattr(serial_execution, "project_compile_surface", lambda _root: next(surfaces))
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    with pytest.raises(SimulationRecordIntegrityError, match="compile inputs changed"):
        SimulationRecord(_executor(build_root, counters)).run(
            NewSimulationRecordRunRequest(
                plan,
                project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                _admission(),
            )
        )

    assert counters["compile"] == 1
    assert counters["launch"] == 0
    store = SimulationRecordStore(invocation / "targets/sim/record")
    assert len(store.scan().interrupted) == 1
    assert not tuple(store.root.glob("work-items/*/result.json"))


def test_scope_stop_after_real_hook_retains_firing_without_simulator_launch(tmp_path, monkeypatch):
    plan, disclosures = _pre_sim_plan("immutable", ("echo actual-hook",), None)
    # A 1s run timeout must not shorten the Pre-Sim Commands' own timeout (#696).
    store, project, build, request = _published_request(
        tmp_path, plan.manifest, policy=SimulationRecordPolicy(timeout_seconds=1)
    )
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    executor = _executor(build, counters, disclosures=disclosures)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_a, **_k: _handle(project)),
    )
    monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _handle: {})
    original_hook = serial_execution.run_pre_sim_commands
    timeouts: list[int] = []
    stopped = False

    def stop_after_hook(*args, **kwargs):
        nonlocal stopped
        timeouts.append(kwargs["timeout_s"])
        evidence = original_hook(*args, **kwargs)
        stopped = True
        return evidence

    monkeypatch.setattr(serial_execution, "run_pre_sim_commands", stop_after_hook)
    scope = SupervisedExecutionScope(None, project / ".booley", lambda: stopped)
    with supervised_execution_scope(scope), pytest.raises(PreSimScopeStoppedError):
        executor.execute(request)
    assert counters["launch"] == 0
    assert timeouts == [DEFAULT_TIMEOUT_S]
    firings = collect_pre_sim_firings(store.manifest_path)
    assert len(firings) == 1
    assert firings[0].document["returncode"] == 0
    assert firings[0].document["stdout_tail"] == "actual-hook\n"
    assert not (request.attempt_directory.parent.parent / "result.json").exists()


def test_request_replacement_reauthenticates_manifest_and_attempt(tmp_path, monkeypatch):
    from booley.flows.sim.record import pre_sim_evidence

    original = pre_sim_evidence.manifest_digest
    calls = []

    def authenticated_digest(manifest):
        calls.append(manifest)
        return original(manifest)

    monkeypatch.setattr(pre_sim_evidence, "manifest_digest", authenticated_digest)
    first = _hook_publication_request(tmp_path / "first")
    second = _hook_publication_request(tmp_path / "second", ("gamma",), invocation=2)
    copied = replace(first, pre_sim_firing_published=lambda _key: None)
    changed = replace(
        first,
        store=second.store,
        manifest=second.manifest,
        work_item=second.work_item,
        attempt_id=second.attempt_id,
        attempt_ordinal=second.attempt_ordinal,
        attempt_directory=second.attempt_directory,
        producer_invocation_id=2,
    )
    assert len(calls) == 4
    assert len({id(r.pre_sim_decoder) for r in (first, second, copied, changed)}) == 4
    assert first.pre_sim_decoder.manifest_sha256 != changed.pre_sim_decoder.manifest_sha256
    _publish_request_hook(first)
    _publish_request_hook(changed, "gamma")
    assert len(calls) == 4
    records = collect_pre_sim_firings(changed.store.manifest_path)
    assert records[0].document["manifest_sha256"] == changed.pre_sim_decoder.manifest_sha256
    assert records[0].document["producer_invocation_id"] == 2
    with pytest.raises(SimulationRecordIntegrityError):
        _publish_request_hook(replace(first, producer_invocation_id=2), "beta", 2)
    assert not (first.attempt_directory / "pre-sim/0002.json").exists()


@pytest.mark.parametrize(
    "corruption", ["manifest", "producer", "selection", "ordinal", "status", "item"]
)
def test_request_publication_rejects_changed_attempt_after_success(tmp_path, corruption):
    request = _hook_publication_request(tmp_path)
    published = []
    request = replace(request, pre_sim_firing_published=published.append)
    _publish_request_hook(request)
    first_bytes = (request.attempt_directory / "pre-sim/0001.json").read_bytes()
    first_publication = published.copy()
    if corruption == "item":
        other = _hook_publication_request(tmp_path / "other", ("gamma",))
        request = replace(request, work_item=other.work_item)
    if corruption in {"manifest", "producer"}:
        path = request.attempt_directory / "attempt.json"
        document = json.loads(path.read_bytes())
        key = "manifest_sha256" if corruption == "manifest" else "producer_invocation_id"
        document[key] = "sha256:" + "0" * 64 if corruption == "manifest" else 2
        path.write_bytes(canonical_json_bytes(document))
    name = "outside" if corruption == "selection" else "beta"
    ordinal = 0 if corruption == "ordinal" else 2
    status = "timed_out" if corruption == "status" else "passed"
    expected = {
        "manifest": "hook attempt does not bind its frozen workload",
        "item": "hook attempt does not bind its frozen workload",
        "producer": "Pre-Sim Commands evidence owner disagrees",
        "selection": "selection is outside workload",
        "ordinal": "invalid ordinal",
        "status": "timed out hook cannot supply an exit code",
    }
    with pytest.raises(SimulationRecordIntegrityError, match=expected[corruption]):
        _publish_request_hook(request, name, ordinal, status)
    assert published == first_publication
    assert len(published) == 1
    assert (request.attempt_directory / "pre-sim/0001.json").read_bytes() == first_bytes
    assert sorted(p.name for p in (request.attempt_directory / "pre-sim").iterdir()) == [
        "0001.json"
    ]


def _project_relative_hook_simulation_record(tmp_path: Path, route: str, configured: str):
    project = tmp_path / "project"
    project.mkdir()
    (project / "hook.py").write_text(
        "import json, os\n"
        "print(json.dumps(dict(cwd=os.getcwd(), root=os.environ['BOOLEY_PROJECT_ROOT'], "
        "run=os.environ['BOOLEY_RUN_CWD'])))\n",
        encoding="utf-8",
    )
    command = shlex.quote(sys.executable) + " hook.py"
    config = project / ".booley"
    config.mkdir()
    (config / "booley.toml").write_text(
        "[flows.sim]\nrun_cwd = "
        + json.dumps(configured)
        + "\npre_run_commands = "
        + json.dumps([command])
        + "\n",
        encoding="utf-8",
    )
    templated = "{" in configured
    if not templated:
        (project / configured).mkdir(parents=True)

    def configure(document):
        document["workload"]["run_cwd"] = {
            "configured": configured,
            "kind": "templated" if templated else "literal",
            "placeholders": ["test", "attempt"] if templated else [],
        }
        document["work_items"][0]["run_directory"] = {
            "configured": configured,
            "kind": "templated" if templated else "literal",
            "collision_template": "runs/test/attempt" if templated else configured,
        }

    access = "legacy-per-test" if route == "legacy-per-test" else "immutable"
    plan, disclosures = _pre_sim_plan(access, (command,), configure)
    return project, plan, disclosures


@pytest.mark.parametrize("route", ["shared", "legacy-per-test"])
@pytest.mark.parametrize("configured", ["runs/literal", "runs/{test}/{attempt}"])
def test_simulation_record_pre_sim_project_relative_hook_uses_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, configured: str
) -> None:
    project, plan, disclosures = _project_relative_hook_simulation_record(
        tmp_path, route, configured
    )
    store, _project, build, request = _published_request(tmp_path, plan.manifest)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _handle: {})
    result = _executor(build, counters, disclosures=disclosures).execute(request)
    firings = store.pre_sim_references(request.attempt_directory)
    firing = json.loads((request.attempt_directory / firings[0]["path"]).read_text())
    assert firing["status"] == "passed", firing
    captured = json.loads(firing["stdout_tail"])
    assert Path(captured["cwd"]).resolve() == project.resolve()
    assert Path(captured["root"]).resolve() == project.resolve()
    run = Path(captured["run"])
    if route != "legacy-per-test":
        attempt = json.loads((request.attempt_directory / "attempt.json").read_text())
        assert run.resolve() == Path(attempt["run_directory"]["resolved"]).resolve()
        assert run.resolve() != project.resolve()
    elif "{" not in configured:
        assert run.resolve() == (project / configured).resolve()
    else:
        assert run.is_absolute() and run.resolve().is_relative_to(project.resolve())
    assert result.document["grade"] == "pass"
    assert counters["launch"] == 1
    if "{" in configured:
        # Booley-created parents of the templated attempt directory are cleaned up.
        assert list((project / "runs").iterdir()) == []
    builds = list(store.root.rglob("build-result.json"))
    assert len(builds) == 1
    assert result.document["build_result"]["sharing"] == (
        "shared_variant" if route == "shared" else "private_work_item"
    )


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_store_follows_only_the_booley_dir_entry(tmp_path, linked):
    """#2191: a Simulation Record store crosses the Booley Directory link, never a link below it."""
    project = tmp_path / "project"
    project.mkdir()
    entry = project / ".booley"
    symlinked_booley_dir(project, tmp_path / "external")
    if linked == "below":
        # A link below the entry (flow-reports) stays refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink_or_skip(entry / "flow-reports", outside, target_is_directory=True)
    else:
        (entry / "flow-reports").mkdir()
    store = SimulationRecordStore(
        entry / "flow-reports" / "sim" / "1" / "targets" / "sim" / "record", booley_dir=entry
    )
    manifest = _manifest_for(("smoke",))

    if linked == "below":
        with pytest.raises(SimulationRecordIntegrityError, match="contains a link"):
            store.publish_manifest(manifest)
        return
    digest = store.publish_manifest(manifest)
    assert store.load_manifest().document == manifest.document
    assert store.manifest_sha256() == digest
    store.regenerate_summary()
    assert json.loads(store.summary_path.read_text())["complete"] is False
    with store.mutation_lock():
        assert (store.root / ".lock").exists()


@pytest.mark.parametrize(
    "defect",
    ["manifest", "workload", "work_item", "attempt", "replace", "symlink", "hardlink", "resize"],
)
def test_scan_fails_closed_on_a_defective_complete_result(tmp_path: Path, defect: str) -> None:
    """The scan binds a complete result to its manifest, item and final attempt, and to one file."""
    (tmp_path / ".booley").mkdir()
    completed = _completed(tmp_path)
    path = completed.store.work_item_directory(completed.item_id) / "result.json"
    message: str | None = "result identity disagrees"
    if defect in {"manifest", "workload", "work_item", "attempt"}:
        result = _read(path)
        if defect in {"manifest", "workload"}:
            # Not a production name: the document key is built as "<defect>_sha256".
            result[f"{defect}_sha256"] = _BAD_DIGEST
        elif defect == "work_item":
            result["work_item_id"] = "item:9999:" + "e" * 16
        else:
            result["attempt_id"] = str(uuid.uuid4())
            message = "result"
        _write(path, result)
    else:
        original = path.with_name("original-result.json")
        path.rename(original)
        if defect == "replace":
            replacement = json.loads(original.read_bytes())
            replacement["attempt_id"] = "0" * 32
            path.write_bytes(canonical_json_bytes(replacement))
        elif defect == "symlink":
            path.symlink_to(original.name)
        elif defect == "hardlink":
            os.link(original, path)
        else:
            path.write_bytes(b"x" * (RECORD_MAX_BYTES + 1))
        message = None
    with pytest.raises(SimulationRecordIntegrityError, match=message):
        completed.store.scan()


@pytest.mark.parametrize("marker", ["owned", "missing", "mismatched"])
def test_stale_run_cleanup_only_removes_a_tree_the_exact_owner_marked(
    tmp_path: Path, marker: str
) -> None:
    identity = _run_identity()
    run = RunDirectory(
        tmp_path / "run", str(tmp_path / "run"), True, tmp_path / "run.lock", tmp_path
    )
    if marker == "owned":
        with claimed_run_directory(run, identity=identity) as directory:
            (directory / "nested").mkdir()
            (directory / "nested/output.log").write_text("stale", encoding="utf-8")
            owned_marker = (directory / ".booley-simulation-attempt.json").read_bytes()
        run.path.mkdir()
        (run.path / ".booley-simulation-attempt.json").write_bytes(owned_marker)
        (run.path / "stale.log").write_text("stale", encoding="utf-8")
        assert cleanup_interrupted_run_directory(run, identity=identity) is True
        assert not run.path.exists()
        return
    run.path.mkdir()
    payload = run.path / "preserve.log"
    payload.write_text("mine", encoding="utf-8")
    if marker == "mismatched":
        wrong = _run_identity()
        (run.path / ".booley-simulation-attempt.json").write_bytes(canonical_json_bytes(wrong))
    with pytest.raises(
        SimulationRecordIntegrityError,
        match=r"refusing to clean unowned run directory: .*; "
        r"move the directory elsewhere, which keeps its files, then retry",
    ):
        cleanup_interrupted_run_directory(run, identity=identity)
    assert payload.read_text(encoding="utf-8") == "mine"
    run.path.rename(tmp_path / "kept")
    assert cleanup_interrupted_run_directory(run, identity=identity) is False
    assert (tmp_path / "kept/preserve.log").read_text(encoding="utf-8") == "mine"


def test_attempt_is_published_before_real_child_waiter_and_cancel_retires_the_lane(
    tmp_path: Path,
) -> None:
    manifest = _two_item_manifest()
    simulation_record_store = SimulationRecordStore(tmp_path / "record")
    simulation_record_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    registry = ChildExecutionRegistry(manifest, project)
    slots = SlotStore(tmp_path / "slots")
    outer = _outer_claim(slots)
    cancelled = threading.Event()
    admission = replace(
        _managed(slots, outer, holders=1),
        timeout_seconds=_PARALLEL_SYNC_TIMEOUT_S,
        cancellation=cancelled.is_set,
    )
    capacity = _item_capacity(admission, manifest, registry.project_data, lanes=2)
    attempts: list[Path] = []

    def prepare(attempt, child_id, *_entry_digest) -> None:
        attempts.append(attempt.directory / "attempt.json")
        published = SimulationAttempt(
            {
                "$schema": "booley.simulation-attempt/v2",
                "simulation_record_id": manifest.document["simulation_record_id"],
                "manifest_sha256": manifest_digest(manifest),
                "workload_sha256": "sha256:" + "0" * 64,
                "work_item_id": attempt.item["work_item_id"],
                "attempt_id": attempt.attempt_id,
                "attempt_ordinal": attempt.ordinal,
                "producer_invocation_id": 1,
                "build_variant_id": "variant:" + "0" * 64,
                "run_directory": {
                    "kind": "literal",
                    "configured": "run",
                    "resolved": "run",
                    "collision_key": "run",
                    "owned": True,
                },
                "child_execution_id": child_id,
                "child_entry_sha256": None,
                "pre_sim_build_access": "immutable",
                "policy": {"timeout_seconds": None, "no_kill": False, "diagnostic": False},
                "started_at": "2026-09-22T14:00:00Z",
            }
        )
        (attempt.directory / "attempt.json").write_bytes(published.canonical_bytes())

    errors: list[BaseException] = []
    scheduler = BoundedSimulationRecordScheduler(
        capacity,
        allocate=lambda item: _scheduled_attempt(simulation_record_store, item),
        prepare_child=prepare,
        execute=lambda *_args: cancelled.wait(_PARALLEL_SYNC_TIMEOUT_S),
    )
    worker = threading.Thread(
        target=lambda: _capture_error(errors, scheduler.run, manifest.document["work_items"])
    )
    worker.start()
    try:
        _cancel_child_and_assert(
            slots, attempts, registry, simulation_record_store, cancelled, worker
        )
        # Cancelling while the second item waits for its claim legitimately
        # aborts that claim; anything else (e.g. a spurious bounded-shutdown
        # SimulationRecordSchedulingError, #977) is a failure.
        assert all(isinstance(e, (ClaimAbortedError, ClaimLostError)) for e in errors), errors
    finally:
        slots.release(outer)


def _cancel_child_and_assert(
    slots, attempts, registry, simulation_record_store, cancelled, worker
) -> None:
    _wait_until(lambda: len(slots.snapshot(CLASS_MEMORY)[1]) == 1)
    assert attempts and attempts[0].is_file()
    # The attempt names its lane execution, which already has a context and a waiting record.
    attempt = json.loads(attempts[0].read_bytes())
    lane = execution_paths(
        ExecutionId(attempt["child_execution_id"]), booley_dir=registry.project_data
    )
    assert lane.context.is_file() and read_json(lane.record)["state"] == "waiting"
    cancelled.set()
    worker.join(timeout=_PARALLEL_SYNC_TIMEOUT_S)
    assert not worker.is_alive()
    assert registry.is_terminal(ExecutionId(attempt["child_execution_id"]))
    assert slots.snapshot(CLASS_MEMORY)[1] == []


def _capture_error(errors: list[BaseException], call, *args) -> None:
    try:
        call(*args)
    except BaseException as exc:  # noqa: BLE001 -- thread ferry for assertion
        errors.append(exc)


def _pre_sim_adapter(
    command: str, status: str, detail: str, returncode: int | None, *, infrastructure: bool = False
) -> NativeExecution:
    """An injected adapter whose Pre-Sim evidence has ``status``; a merge after it must not run."""

    class PreSimAdapter(NativeExecution):
        def run(self, request):
            self.runs.append(request)
            evidence = PreSimEvidence(
                (command,), (request.test.name,), status, 0.2, detail, returncode=returncode
            )
            return SimulationRunResult(
                "elab_error", evidence.detail, evidence, infrastructure_error=infrastructure
            )

        def command(self, request):
            raise AssertionError(f"merge must not run after a Pre-Sim {status}")

    return PreSimAdapter()


def test_coverage_aggregate_preserves_pre_sim_failure_observation(tmp_path: Path) -> None:
    outcome, store = _run_coverage_simulation_record(
        tmp_path,
        _pre_sim_adapter("prepare-vectors", "failed", "vector generator rejected input", 1),
        pre_sim_commands=("prepare-vectors",),
    )

    assert outcome.complete is True
    result = store.scan().items[0].result
    assert result is not None
    observations = result.document["observations"]
    assert observations[0]["execution"] == "completed"
    assert observations[0]["detail"]["reason"] == (
        "Pre-Sim Commands failed (failed): vector generator rejected input"
    )

    firings = collect_pre_sim_firings(store.manifest_path)
    assert [f.document["ordinal"] for f in firings] == [1, 2]
    assert [f.document["test_names"] for f in firings] == [("reset",), ("wrap",)]
    assert all(f.document["status"] == "failed" for f in firings)
    assert all(f.document["command_count"] == 1 for f in firings)
    assert all(f.document["returncode"] == 1 for f in firings)
    assert all(f.document["producer_invocation_id"] == 1 for f in firings)
    assert all(f.terminal and f.reference in result.document["evidence"] for f in firings)


def test_coverage_aggregate_preserves_spawn_error_detail_and_aborts(tmp_path: Path) -> None:
    # Private: `detail["evaluation"]` is only on this exception type, which no public path re-exports.
    with pytest.raises(_CoverageAggregateError, match="missing-vector-generator") as raised:
        _run_coverage_simulation_record(
            tmp_path,
            _pre_sim_adapter(
                "missing-vector-generator",
                "spawn_error",
                "missing-vector-generator: command not found",
                None,
                infrastructure=True,
            ),
            pre_sim_commands=("missing-vector-generator",),
        )

    assert raised.value.detail["evaluation"] == "not_requested"
    nested = next(
        (tmp_path / "reports/1/targets/sim_0").glob(
            "record/work-items/*/attempts/*/coverage-record/coverage.json"
        )
    )
    document = json.loads(nested.read_text(encoding="utf-8"))
    assert document["collection"]["status"] == "collector_error"
    never_run = document["tests"]["runs"][1]
    assert never_run["execution"] == "not_run"
    assert never_run["failure_kind"] == "infrastructure"
    assert "termination" not in never_run

    # This injected external adapter has no observed process exit status.
    store = SimulationRecordStore(tmp_path / "reports/1/targets/sim_0/record")
    (firing,) = collect_pre_sim_firings(store.manifest_path)
    assert firing.document["status"] == "spawn_error"
    assert firing.document["returncode"] is None
    assert firing.document["command_count"] == 1
    assert firing.document["test_names"] == ("reset",)
    assert firing.document["producer_invocation_id"] == 1
    assert firing.terminal is False


def test_coverage_rejects_fabricated_hook_with_empty_frozen_commands(tmp_path: Path) -> None:
    class FabricatedHook(NativeExecution):
        def run(self, request):
            evidence = PreSimEvidence(
                ("undeclared-hook",), (request.test.name,), "failed", 0.2, returncode=1
            )
            return SimulationRunResult("elab_error", "injected adapter evidence", evidence)

    with pytest.raises(SimulationRecordIntegrityError, match="command count disagrees"):
        _run_coverage_simulation_record(tmp_path, FabricatedHook())
    simulation_record = tmp_path / "reports/1/targets/sim_0/record"
    assert list(simulation_record.glob("work-items/*/attempts/*/attempt.json"))
    assert not list(simulation_record.glob("work-items/*/attempts/*/pre-sim/*.json"))


def test_resume_keeps_run_directories_of_an_unrecoverable_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume recovers every lane first: one that cannot be proven terminal keeps all runs."""
    from tests.flows.sim.simulation_record_test_support import (
        _interrupted_simulation_record,
        _SimulationRecordAdmissionEndpoint,
    )

    case = _interrupted_simulation_record(tmp_path, monkeypatch, None)
    project_data = case.project / ".booley"
    attempts = {
        item_id: json.loads(
            next(
                (case.store.work_item_directory(item_id) / "attempts").glob("*/attempt.json")
            ).read_bytes()
        )
        for item_id in case.store.scan().complete
    }
    # The outer lane's item comes first in manifest order, ahead of the child lanes' items.
    interrupted = [
        item_id for item_id, doc in attempts.items() if doc["child_execution_id"] is None
    ]
    interrupted += [item_id for item_id, doc in attempts.items() if doc["child_execution_id"]][:1]
    runs = []
    for item_id in interrupted:
        (case.store.work_item_directory(item_id) / "result.json").unlink()
        lane = attempts[item_id]["child_execution_id"]
        run, _document = _own_attempt_run_directory(tmp_path, case.store, item_id, lane)
        runs.append(run)
        if lane is not None:
            # The crash left this lane's execution unfinished.
            lane_record = execution_paths(ExecutionId(lane), booley_dir=project_data).record
            atomic_write_json(lane_record, {"state": "waiting", "tree_terminal": False})
    assert case.store.scan().interrupted == tuple(interrupted)

    case.fault_enabled[0] = False
    monkeypatch.setattr(ChildExecutionRegistry, "cancel", lambda _self, _id: False)
    with (
        AdmissionGate(_SimulationRecordAdmissionEndpoint(case.slots, None)).enter() as admission,
        pytest.raises(SimulationRecordIntegrityError, match="recovery is incomplete"),
    ):
        case.simulation_record.run(
            ResumeSimulationRecordRunRequest(
                case.validated,
                case.plan,
                case.project,
                case.invocation.parent,
                SimulationRecordPolicy(),
                case.invocation,
                admission,
            )
        )

    assert all(run.exists() for run in runs)


def test_foreign_attempt_result_fails_closed_at_the_read_boundary(tmp_path: Path) -> None:
    class _ForeignAttemptExecutor(_NoEdaExecutor):
        def execute(self, request: WorkExecutionRequest) -> SimulationResult:
            result = super().execute(request)
            return SimulationResult({**result.document, "attempt_id": str(uuid.uuid4())})

    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan=create_simulation_record_plan(_manifest_for(("smoke",))),
        project_root=project,
        report_root=None,
        policy=SimulationRecordPolicy(),
        invocation_directory=tmp_path / "reports" / "sim" / "1",
        admission=_admission(),
    )

    with pytest.raises(SimulationRecordIntegrityError, match="final published attempt"):
        SimulationRecord(_ForeignAttemptExecutor()).run(request)


def test_mark_terminal_never_replaces_the_terminal_record_recovery_writes_mid_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _two_item_manifest()
    registry = ChildExecutionRegistry(manifest, tmp_path)
    execution_id = ExecutionId(uuid.uuid4().hex)
    work_item_id = str(manifest.document["work_items"][0]["work_item_id"])  # type: ignore[index]
    prepared = registry.prepare(
        execution_id,
        work_item_id=work_item_id,
        attempt_id=str(uuid.uuid4()),
        attempt_ordinal=1,
        attempt_directory=tmp_path / "attempt",
        parent_execution_id=ExecutionId(uuid.uuid4().hex),
    )

    replaced = run_with_recovery_between_read_and_write(
        monkeypatch,
        execution_id,
        tmp_path,
        lambda: registry.mark_terminal(prepared, "record_error"),
    )

    assert "terminal" not in replaced
    final = read_json(execution_paths(execution_id, booley_dir=tmp_path).record)
    assert final is not None
    assert final["state"] == "terminal"
    assert final["tree_terminal"] is True


def test_later_work_item_disclosure_must_match_the_retained_preparation(tmp_path: Path) -> None:
    """A later item launches from the first item's preparation, so its plan must agree (#4843)."""
    retained = _packaged_source_disclosure(b"retained wheel bytes")
    planned_later = _packaged_source_disclosure(b"later item planned different bytes")
    document = json.loads(canonical_json_bytes(_manifest_for(("alpha", "beta")).document))
    document.pop("fingerprints")
    document["planning_disclosures"] = [retained, planned_later]
    manifest = finalize_manifest(document)
    store, project, build_root, alpha = _published_request(tmp_path, manifest)
    beta = _request(store, manifest, manifest.document["work_items"][1], project, invocation=1)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    executor = _executor(
        build_root, counters, disclosures={"alpha": retained, "beta": planned_later}
    )

    executor.execute(alpha)
    with pytest.raises(
        SimulationRecordIntegrityError,
        match="prepared generator source closure disagrees with record plan",
    ):
        executor.execute(beta)

    assert counters == {"compile": 1, "durable_reuse": 0, "launch": 1}
