"""Process-death matrix for the ordinary-HDL serial publication protocol."""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.flows.sim.execution.contract import (
    PreSimEvidence,
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.record import collect_pre_sim_firings
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.coverage_execution import (
    CoverageAggregateExecutor,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
)
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.record_reports import write_compatibility_projection
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _coverage_simulation_record_plan,
    _validated_resume,
)
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.flows.sim.test_simulation_record_manifest_codec import _manifest
from tests.time_budget import hang_guard_s

_DURABLE_BOUNDARIES = (
    "manifest_commit",
    "simulation_attempt",
    "build_attempt",
    "bundle_evidence",
    "build_result",
    "runtime_inputs",
    "execution_evidence",
    "simulation_result",
    "summary_replace",
)
_CRASH_POINTS = (
    *(f"{side}:{boundary}" for boundary in _DURABLE_BOUNDARIES for side in ("before", "after")),
)


def _build_execution() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-build-execution/v1",
        "process": {
            "returncode": 0,
            "stdout": "build\n",
            "stderr": "",
            "timed_out": False,
            "duration_s": 0.1,
            "dispatched_unix": 1.0,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
        },
        "build": {
            "ran": True,
            "verdict": "pass",
            "failure_kind": None,
            "elapsed_s": 0.1,
            "output": "build\n",
            "returncode": 0,
            "timed_out": False,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
            "terminal_record": True,
            "reason": "",
            "cache_decision": "",
        },
    }


class _InjectedProcessDeath(BaseException):
    pass


class _CrashOnce:
    def __init__(self, selected: str) -> None:
        self.selected = selected
        self.seen: list[str] = []
        self.tripped = False

    def __call__(self, boundary: str) -> None:
        self.seen.append(boundary)
        if boundary == self.selected and not self.tripped:
            self.tripped = True
            raise _InjectedProcessDeath(boundary)


class _CrashCatalog:
    def __init__(self, project: Path) -> None:
        self.project = project

    def select(self, token: str, *, for_flow: str):
        assert (token, for_flow) == ("sim", "sim")
        return SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            project_root=self.project,
            selector="sim",
            eda_tool="icarus",
        )


class _CrashGroup:
    def __init__(self, build_root: Path, run_log: Path, launches: list[int]) -> None:
        self.build_root = build_root
        self.run_log = run_log
        self.launches = launches
        self.artifact_paths = (build_root / "simv",)
        self.compile_surface = SimpleNamespace(
            project_root=build_root.parent.resolve(),
            authored_paths=(),
            operational_paths=(),
            optional_paths=(),
        )

    def compile(self):
        return SimpleNamespace(passed=True)

    def planning_disclosure(self):
        return {}

    def for_work_item(self, _names, _test_runs):
        return self

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence):
        assert evidence == _build_execution()

    def launch_bundle(self, bundle_root: Path, evidence_root: Path, run_cwd: Path):
        self.launches.append(1)
        assert (bundle_root / "simv").is_file()
        assert evidence_root.name == "execution-evidence"
        assert (run_cwd / "inputs/input.bin").read_bytes() == b"runtime"
        # The engine writes the run log into the evidence root and reports it.
        evidence_root.mkdir(exist_ok=True)
        log = evidence_root / "run.log"
        log.write_bytes(self.run_log.read_bytes())
        test = SimulationTestOutcome("sim", "pass", True, str(log))
        artifact = SimulationArtifactEvidence(
            "live_run_log", str(log), log.stat().st_size, ("sim",)
        )
        return SimulationTargetOutcome(
            "sim",
            "acme:lib:dut:1#sim",
            "tb",
            "icarus",
            True,
            "pass",
            0.1,
            (test,),
            artifacts=(artifact,),
        )


class _CrashExecution:
    def __init__(self, group: _CrashGroup) -> None:
        self.group = group

    @contextmanager
    def ordinary_group(self, handle, names, _test_runs=None):
        del handle
        assert names == ()
        yield self.group


def _crash_manifest(*, shared: bool = True):
    """Crash fixture manifest: Shared Simulator Bundle route or private build."""
    document = _manifest()
    document.pop("fingerprints")
    identity = {"source_artifact_path": "input.bin", "destination": "inputs/input.bin"}
    declaration_id = (
        "sha256:" + hashlib.sha256(canonical_json_bytes(identity).rstrip(b"\n")).hexdigest()
    )
    document["workload"]["runtime_inputs"] = [  # type: ignore[index]
        {"declaration_id": declaration_id, **identity}
    ]
    # Routing reads only ``pre_sim_build_access``: immutable access shares one
    # Simulator Bundle, legacy-per-test access compiles a private per-attempt build.
    # Planning derives ``sharing_eligible`` from the same access, so keep them aligned.
    access = "immutable" if shared else "legacy-per-test"
    document["workload"]["pre_sim_build_access"] = access  # type: ignore[index]
    document["build_variants"][0]["sharing_eligible"] = shared  # type: ignore[index]
    return finalize_manifest(document)


def _crash_environment(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"runtime")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    return project, build_root, run_log


def _resume_crashed_simulation_record(
    simulation_record, request, store, manifest, plan, crash_point
):
    if crash_point == "before:manifest_commit":
        assert not store.manifest_path.exists()
        return simulation_record.run(request)
    assert store.manifest_path.is_file()
    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)), (), ()
    )
    return simulation_record.run(
        ResumeSimulationRecordRunRequest(
            validated,
            plan,
            request.project_root,
            request.report_root,
            request.policy,
            request.invocation_directory,
            request.admission,
        )
    )


def _assert_crash_recovery(outcome, store, crash_point, launches, crash) -> None:
    recovered = store.scan().items[0]
    committed = crash_point in {
        "after:simulation_result",
        "before:summary_replace",
        "after:summary_replace",
    }
    pre_attempt = crash_point in {"before:manifest_commit", "after:manifest_commit"}
    assert outcome.complete is True
    assert recovered.state == "complete"
    assert recovered.attempt_count == (1 if committed or pre_attempt else 2)
    assert len(launches) == 1 if committed else 1 <= len(launches) <= 2
    assert crash.tripped is True
    assert crash_point in crash.seen


@pytest.mark.parametrize("route", ("shared", "private"))
@pytest.mark.parametrize("crash_point", _CRASH_POINTS)
def test_serial_publication_boundary_resume_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    route: str,
) -> None:
    """Every durable prefix resumes on both build routes; only a committed Result suppresses rerun."""
    crash = _CrashOnce(crash_point)
    case = _serial_case(tmp_path / "case", monkeypatch, route == "shared", crash)
    manifest, store = case.manifest, case.store

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        case.record.run(case.request)
    outcome = _resume_crashed_simulation_record(
        case.record, case.request, store, manifest, case.plan, crash_point
    )
    _assert_crash_recovery(outcome, store, crash_point, case.launches, crash)
    # The route axis must really reach its build: private builds live under the attempt.
    private_builds = list(store.root.glob("work-items/*/attempts/*/private-build"))
    assert bool(private_builds) == (route == "private")
    from booley.flows.sim.record.codec import MANIFEST_SCHEMA, RESULT_SCHEMA

    assert store.load_manifest().document["$schema"] == MANIFEST_SCHEMA
    result_paths = list(store.root.glob("work-items/*/result.json"))
    assert len(result_paths) == len(manifest.document["work_items"])
    for path in result_paths:
        result = json.loads(path.read_bytes())
        assert result["$schema"] == RESULT_SCHEMA
        assert result["bundle_verification"]["verified_after_exit"] is True
        assert all("test_run" in observation for observation in result["observations"])
        attempt = (
            path.parent / "attempts" / f"{result['attempt_ordinal']:04d}-{result['attempt_id']}"
        )
        (log,) = [item for item in result["evidence"] if item["kind"] == "live_run_log"]
        assert log["path"] == "execution-evidence/run.log"
        kept = (attempt / log["path"]).read_bytes()
        assert log["sha256"] == "sha256:" + hashlib.sha256(kept).hexdigest()
    assert not list(store.root.glob("work-items/*/attempts/*/snapshot"))


@pytest.mark.parametrize(
    "crash_point",
    [
        f"{side}:{boundary}"
        for boundary in ("simulation_attempt", "runtime_inputs")
        for side in ("before", "after")
    ],
)
def test_coverage_aggregate_boundary_resume_matrix(tmp_path: Path, crash_point: str) -> None:
    """The coverage aggregate's own durable boundaries resume to one terminal Result."""
    crash = _CrashOnce(crash_point)
    record, request, resume, store, _target_directory = _coverage_case(tmp_path / "case", crash)

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        record.run(request)
    outcome = record.run(resume)

    assert outcome.complete is True
    assert crash.tripped is True
    # The crashed attempt stays on disk; the resume adds exactly one.
    assert store.scan().items[0].attempt_count == 2
    assert len(list(store.root.glob("work-items/*/result.json"))) == 1


# --- Process death in the middle of a durable write (#4213) -------------------------

_KILLED = 57
_STAGED = ".booley-staged"
_needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="the kill matrix forks a worker")


class _KillAtWrite:
    """Publication checkpoint: arm on ``before:<boundary>``, then die inside the next write.

    ``mid-write`` kills inside the first byte-stream write after arming, having
    written half of it; ``after-link`` kills right after the next hard link
    claimed a final name. Either way the worker exits like a SIGKILL: no
    ``finally`` and no cleanup. The path involved goes to ``side_file`` first.
    """

    def __init__(
        self, boundary: str, phase: str, side_file: Path, *, hook_path_opens: bool = False
    ) -> None:
        self.boundary = boundary
        self.phase = phase
        self.side_file = side_file
        # The Acceptance Journal and the Development State write through
        # ``Path.open``/``Path.write_text``, not ``os.fdopen``.
        self.hook_path_opens = hook_path_opens
        self.test_process = os.getpid()
        self.seen: list[str] = []

    def __call__(self, name: str) -> None:
        self.seen.append(name)
        # The resume in the test process shares this checkpoint and must not die.
        if name == f"before:{self.boundary}" and os.getpid() != self.test_process:
            self._arm()

    def _arm(self) -> None:
        paths: dict[int, str] = {}
        real_open, real_fdopen, real_link = os.open, os.fdopen, os.link
        side_file = self.side_file

        real_io_open = io.open

        def die(path: object) -> None:
            with real_io_open(side_file, "w", encoding="utf-8") as record:
                record.write(str(path))
            os._exit(_KILLED)

        def remembering_open(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            paths[descriptor] = str(path)
            return descriptor

        def dying_fdopen(descriptor, mode="r", *args, **kwargs):
            writer = real_fdopen(descriptor, mode, *args, **kwargs)
            if "b" not in mode or "w" not in mode:
                return writer
            return _HalfWriter(writer, lambda: die(paths.get(descriptor, "")))

        def dying_link(source, destination, **kwargs):
            real_link(source, destination, **kwargs)
            die(destination)

        def dying_io_open(path, mode="r", *args, **kwargs):
            writer = real_io_open(path, mode, *args, **kwargs)
            if "w" not in mode:
                return writer
            return _HalfWriter(writer, lambda: die(paths.get(path, path)))

        os.open = remembering_open  # type: ignore[assignment]
        if self.phase == "mid-write":
            os.fdopen = dying_fdopen  # type: ignore[assignment]
            if self.hook_path_opens:
                io.open = dying_io_open  # type: ignore[assignment]
        else:
            os.link = dying_link  # type: ignore[assignment]


class _HalfWriter:
    """A binary writer that writes half of its first chunk, then runs ``die``."""

    def __init__(self, writer, die) -> None:
        self._writer = writer
        self._die = die

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def write(self, data: bytes) -> None:
        self._writer.write(data[: len(data) // 2])
        self._writer.flush()
        self._die()

    def flush(self) -> None:
        self._writer.flush()


def _run_killed(run, trace: Path) -> None:
    """Run ``run`` in a forked worker and require it to die inside its write.

    The worker has none of the test's own threads. A worker that returns, or
    fails for another reason, fails the test with its traceback; a worker that
    hangs is killed at a deadline so none outlives the test.
    """
    pid = os.fork()
    if pid == 0:
        try:
            try:
                run()
            except BaseException:  # noqa: BLE001 - the worker reports every failure, then exits
                trace.write_text(traceback.format_exc(), encoding="utf-8")
                os._exit(4)
        finally:
            os._exit(3)
    deadline = time.monotonic() + hang_guard_s()
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the killed worker hung")
        time.sleep(0.02)
    code = os.waitstatus_to_exitcode(status)
    detail = trace.read_text(encoding="utf-8") if trace.exists() else "the run returned"
    assert code == _KILLED, f"the worker exited {code} before reaching its write: {detail}"


def _assert_killed_inside_the_write(
    side_file: Path, phase: str, target_directory: Path, *, staging: tuple[str, ...] = (_STAGED,)
) -> None:
    """The worker died in a write below the Target directory, never under a reader's name.

    ``staging`` is what a hidden staged file may end with: the durable helper's
    suffix, plus ``.tmp`` where the summary or the Acceptance Journal stage.
    """
    path = Path(side_file.read_text(encoding="utf-8"))
    assert path.is_relative_to(target_directory), path
    if phase == "after-link":
        assert path.is_file()
        assert path.stat().st_nlink == 2
        return
    assert not path.exists() or (path.name.startswith(".") and path.name.endswith(staging)), path


def _check_every_case(tmp_path: Path, cases, check) -> None:
    """Run ``check(case_directory, *case)`` for each case, naming the case that failed."""
    for index, case in enumerate(cases):
        try:
            check(tmp_path / f"case-{index}", *case)
        except Exception as exc:
            raise AssertionError(f"kill case {case}: {exc!r}") from exc


def _staging_of(boundary: str) -> tuple[str, ...]:
    return (_STAGED, ".tmp") if boundary == "summary_replace" else (_STAGED,)


@dataclass
class _SerialCase:
    record: SimulationRecord
    request: NewSimulationRecordRunRequest
    store: SimulationRecordStore
    manifest: object
    plan: object
    launches: list[int]
    target_directory: Path


def _serial_case(root: Path, monkeypatch: pytest.MonkeyPatch, shared: bool, checkpoint):
    manifest = _crash_manifest(shared=shared)
    plan = create_simulation_record_plan(manifest)
    root.mkdir()
    project, build_root, run_log = _crash_environment(root)
    launches: list[int] = []
    group = _CrashGroup(build_root, run_log, launches)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: _CrashCatalog(project),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _CrashExecution(group),  # type: ignore[arg-type,return-value]
        publication_checkpoint=checkpoint,
    )
    invocation = root / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan=plan,
        project_root=project,
        report_root=invocation.parent,
        policy=SimulationRecordPolicy(),
        invocation_directory=invocation,
        admission=_admission(),
    )
    return _SerialCase(
        SimulationRecord(executor, publication_checkpoint=checkpoint),
        request,
        SimulationRecordStore(invocation / "targets" / "sim" / "record"),
        manifest,
        plan,
        launches,
        invocation / "targets" / "sim",
    )


_TIMING_KEYS = {
    "attempt_id",
    "attempt_ordinal",
    "producer_invocation_id",
    "finished_at",
    "elapsed_seconds",
    # What the attempt's own Simulator Bundle binds: a fresh identity per attempt.
    "bundle_id",
    "build_result",
    "bundle_manifest_sha256",
    # How a runtime input reached the run directory: the killed attempt left it there.
    "method",
    "owned",
}


_ROOT_KEYS = {"manifest_sha256", "simulation_record_id", "work_item_id", "workload_sha256"}


def _comparable(value, *, per_root: bool = False):
    """A Result without attempt identity and timing; references reduced to their digests.

    ``per_root`` also drops what embeds the project's absolute path (the
    record, work item and workload identities) and reduces each reference to
    its kind, for records built under different roots.
    """
    if isinstance(value, list):
        return [_comparable(item, per_root=per_root) for item in value]
    if not isinstance(value, dict):
        return value
    if "sha256" in value and "path" in value:
        return {"kind": value.get("kind")} if per_root else {"sha256": value["sha256"]}
    snapshot = value.get("executable_snapshot")
    if isinstance(snapshot, dict):
        value = {
            **value,
            "executable_snapshot": {k: v for k, v in snapshot.items() if k != "manifest"},
        }
    dropped = _TIMING_KEYS | (_ROOT_KEYS if per_root else set())
    return {
        key: _comparable(item, per_root=per_root)
        for key, item in value.items()
        if key not in dropped
    }


def _comparable_results(store: SimulationRecordStore, *, per_root: bool = False) -> list[object]:
    paths = sorted(store.root.glob("work-items/*/result.json"))
    return [_comparable(json.loads(path.read_bytes()), per_root=per_root) for path in paths]


def _assert_no_staged_files(target_directory: Path) -> None:
    leftovers = [
        path.name
        for path in target_directory.rglob("*")
        if path.name.endswith(_STAGED)
        or (path.name.startswith(".summary.json.") and path.suffix == ".tmp")
    ]
    assert leftovers == []


def _serial_kill_case(
    root: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, route: str, phase: str
) -> None:
    root.mkdir()
    side_file = root / "side.txt"
    kill = _KillAtWrite(boundary, phase, side_file)
    killed = _serial_case(root / "killed", monkeypatch, route == "shared", kill)
    _run_killed(lambda: killed.record.run(killed.request), root / "trace.txt")
    _assert_killed_inside_the_write(
        side_file, phase, killed.target_directory, staging=_staging_of(boundary)
    )

    crash_point = "before:manifest_commit" if not killed.store.manifest_path.exists() else "x"
    outcome = _resume_crashed_simulation_record(
        killed.record, killed.request, killed.store, killed.manifest, killed.plan, crash_point
    )
    baseline = _serial_case(root / "baseline", monkeypatch, route == "shared", None)
    baseline_outcome = baseline.record.run(baseline.request)

    assert outcome.complete is True
    assert baseline_outcome.complete is True
    assert {item.state for item in killed.store.scan().items} == {"complete"}
    _assert_no_staged_files(killed.target_directory)
    assert _comparable_results(killed.store) == _comparable_results(baseline.store)
    assert _comparable_results(killed.store) != []


@_needs_fork
@pytest.mark.timeout(180)
def test_serial_publication_killed_mid_write_resumes_like_an_uninterrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILL inside any durable write leaves nothing a reader trusts, and resume completes.

    Every durable boundary, on both build routes, killed inside its write and right
    after its link (the summary is replaced, never linked).
    """
    cases = [
        (boundary, route, phase)
        for boundary in _DURABLE_BOUNDARIES
        for route in ("shared", "private")
        for phase in ("mid-write", "after-link")
        if not (phase == "after-link" and boundary == "summary_replace")
    ]
    _check_every_case(
        tmp_path, cases, lambda root, *case: _serial_kill_case(root, monkeypatch, *case)
    )


def _fresh_native_execution(_plan, _options, _commands, _access) -> NativeExecution:
    # Binding the snapshot rewrites the executable path, so each attempt needs its own.
    native = NativeExecution()
    runtime = native.build_root / "data/stimulus.bin"
    runtime.parent.mkdir()
    runtime.write_bytes(b"authenticated runtime input")
    return native


def _coverage_case(root: Path, checkpoint):
    root.mkdir()
    plan, target = _coverage_simulation_record_plan(root, runtime_input=True)
    record = SimulationRecord(
        CoverageAggregateExecutor(
            plans={target.handle.identity: target},
            execution_factory=_fresh_native_execution,
            publication_checkpoint=checkpoint,
        ),
        publication_checkpoint=checkpoint,
    )
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    policy = SimulationRecordPolicy()
    request = NewSimulationRecordRunRequest(
        plan, root, invocation.parent, policy, invocation, _admission()
    )
    store = SimulationRecordStore(invocation / "targets/sim_0/record")
    resume = ResumeSimulationRecordRunRequest(
        _validated_resume(store, plan, target, root),
        plan,
        root,
        invocation.parent,
        policy,
        invocation,
        _admission(),
    )
    return record, request, resume, store, invocation / "targets" / "sim_0"


def _coverage_kill_case(root: Path, boundary: str, phase: str) -> None:
    root.mkdir()
    side_file = root / "side.txt"
    kill = _KillAtWrite(boundary, phase, side_file)
    record, request, resume, store, target_directory = _coverage_case(root / "killed", kill)
    _run_killed(lambda: record.run(request), root / "trace.txt")
    _assert_killed_inside_the_write(side_file, phase, target_directory)

    outcome = record.run(resume)
    base_record, base_request, _resume, base_store, _target = _coverage_case(
        root / "baseline", None
    )
    baseline = base_record.run(base_request)

    assert outcome.complete is True
    assert baseline.complete is True
    assert {item.state for item in store.scan().items} == {"complete"}
    _assert_no_staged_files(target_directory)
    assert _comparable_results(store, per_root=True) == _comparable_results(
        base_store, per_root=True
    )
    assert _comparable_results(store) != []


@_needs_fork
def test_coverage_aggregate_killed_mid_write_resumes(tmp_path: Path) -> None:
    """The coverage aggregate's durable writes survive a SIGKILL like the serial executor's."""
    cases = [
        (boundary, phase)
        for boundary in ("simulation_attempt", "runtime_inputs", "coverage_reference")
        for phase in ("mid-write", "after-link")
    ]
    _check_every_case(tmp_path, cases, _coverage_kill_case)


def _pre_sim_kill_case(root: Path, monkeypatch: pytest.MonkeyPatch, access: str) -> None:
    root.mkdir()
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    side_file = root / "side.txt"
    kill = _KillAtWrite("pre_sim_evidence", "mid-write", side_file)
    _run_killed(
        lambda: _successful_pre_sim_record(root, monkeypatch, access, checkpoint=kill),
        root / "trace.txt",
    )
    invocation = root / "reports/1"
    store = SimulationRecordStore(invocation / "targets/sim/record")
    _assert_killed_inside_the_write(side_file, "mid-write", invocation)
    # The worker's patches died with it: the resume in this process needs its own.
    from tests.flows.sim.simulation_record_test_support import _handle

    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(root / "project")),
    )
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.simulation_target_environment",
        lambda _handle: {},
    )

    assert collect_pre_sim_firings(store.manifest_path) == ()
    assert store.scan().interrupted
    resumed, counters = _resume_real_hook_crash(root, invocation, store)
    assert counters["launch"] == 1
    assert len(resumed.pre_sim_firings) == 1
    assert all(
        firing.document["stdout_tail"] == "hook-out\n" for firing in resumed.pre_sim_firings
    )
    assert collect_pre_sim_firings(store.manifest_path) != ()
    _assert_no_staged_files(invocation)


@_needs_fork
def test_pre_sim_evidence_killed_mid_write_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILL inside a Pre-Sim firing's evidence write leaves no firing, and resume reruns it."""
    _check_every_case(
        tmp_path,
        [("immutable",), ("legacy-per-test",)],
        lambda root, access: _pre_sim_kill_case(root, monkeypatch, access),
    )


def _write_projection(path: Path, checkpoint) -> None:
    write_compatibility_projection(
        path, {"target": "sim"}, acceptance_committed=True, publication_checkpoint=checkpoint
    )


@_needs_fork
def test_compatibility_projection_killed_mid_write_is_retryable(tmp_path: Path) -> None:
    """Guard: the projection is replaced atomically, so a kill mid-write leaves no partial file."""
    path = tmp_path / "simulation.json"
    side_file = tmp_path / "side.txt"
    kill = _KillAtWrite("compatibility_projection", "mid-write", side_file)

    _run_killed(lambda: _write_projection(path, kill), tmp_path / "trace.txt")
    _assert_killed_inside_the_write(side_file, "mid-write", tmp_path, staging=(".tmp",))
    assert not path.exists()
    _write_projection(path, kill)

    assert json.loads(path.read_text(encoding="utf-8")) == {"complete": True, "target": "sim"}


def test_compatibility_projection_boundary_is_retryable(tmp_path: Path) -> None:
    # The ``before:`` side is the arm point of the killed-mid-write test above.
    path = tmp_path / "simulation.json"
    crash_point = "after:compatibility_projection"
    crash = _CrashOnce(crash_point)

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        _write_projection(path, crash)
    _write_projection(path, crash)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "complete": True,
        "target": "sim",
    }


def test_invalid_committed_result_fails_closed_before_executor_entry(
    tmp_path: Path,
) -> None:
    document = _manifest()
    document.pop("fingerprints")
    manifest = finalize_manifest(document)
    plan = create_simulation_record_plan(manifest)
    invocation = tmp_path / "reports" / "000001"
    store = SimulationRecordStore(invocation / "targets" / "sim" / "record")
    store.publish_manifest(manifest)
    work_item_id = manifest.document["work_items"][0]["work_item_id"]  # type: ignore[index]
    result_path = store.work_item_directory(work_item_id) / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(b'{"truncated":')
    entered: list[str] = []

    class MustNotExecute:
        def execute(self, request):
            entered.append(request.attempt_id)
            raise AssertionError("invalid Result must fail before EDA execution")

    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)),
        (),
        (),
    )
    request = ResumeSimulationRecordRunRequest(
        validated=validated,
        current_plan=plan,
        project_root=tmp_path,
        report_root=invocation.parent,
        policy=SimulationRecordPolicy(),
        invocation_directory=invocation,
        admission=_admission(),
    )

    with pytest.raises(SimulationRecordIntegrityError):
        SimulationRecord(MustNotExecute()).run(request)  # type: ignore[arg-type]

    assert entered == []


class _FailedBuildGroup:
    artifact_paths: tuple[Path, ...] = ()

    def __init__(self, root: Path, infrastructure: bool) -> None:
        self.build_root = root
        self.infrastructure = infrastructure

    def planning_disclosure(self):
        return {}

    def compile(self):
        return SimpleNamespace(passed=False)

    def finish_build_failure(self):
        failure = (
            SimulationInfrastructureFailure("spawn", "could not spawn", detail="missing")
            if self.infrastructure
            else None
        )
        test = SimulationTestOutcome(
            name="sim",
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail="compile failed",
        )
        return SimulationTargetOutcome(
            target="sim",
            target_identity="acme:lib:dut:1#sim",
            toplevel="tb",
            eda_tool="icarus",
            passed=False,
            verdict="error" if self.infrastructure else "fail",
            elapsed_s=0.1,
            tests=(test,),
            infrastructure_failure=failure,
        )


def _failed_build_plan(failure_path):
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = (  # type: ignore[index]
        "legacy-per-test" if failure_path.startswith("legacy") else "immutable"
    )
    if failure_path.startswith("legacy"):
        from tests.flows.sim.test_simulation_record_manifest_codec import _manifest_work

        workload = document["workload"]
        workload["source_recipe"]["pre_sim_commands"] = ["false"]
        document["build_variants"], document["work_items"] = _manifest_work(
            document["target"], workload["source_recipe"], workload["build_recipe"], workload
        )
    plan = create_simulation_record_plan(finalize_manifest(document))
    return plan


def _failed_build_case(tmp_path, monkeypatch, failure_path, crash):
    plan = _failed_build_plan(failure_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    engine_root = tmp_path / "build"
    engine_root.mkdir()
    infrastructure = failure_path.endswith("spawn")
    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    if failure_path.startswith("legacy"):
        status = "spawn_error" if infrastructure else "failed"
        monkeypatch.setattr(
            "booley.flows.sim.record.serial_execution.run_pre_sim_commands",
            lambda _handle, *, test_names, commands, **_kwargs: PreSimEvidence(
                commands, test_names, status, 0.1, "hook failed", 127 if infrastructure else 7
            ),
        )
    group = _FailedBuildGroup(engine_root, infrastructure)
    execution = SimpleNamespace(
        ordinary_group=lambda *_args: contextmanager(lambda: (yield group))()
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: execution,  # type: ignore[arg-type,return-value]
        publication_checkpoint=crash,
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan, project, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )
    return executor, request, invocation


@pytest.mark.parametrize(
    "boundary",
    [
        "before:build_result",
        "after:build_result",
        "before:simulation_result",
        "after:simulation_result",
    ],
)
@pytest.mark.parametrize(
    ("failure_path", "expected_state"),
    [
        ("legacy_design", "design_failure"),
        ("legacy_spawn", "infrastructure_error"),
        ("compile_design", "design_failure"),
        ("compile_spawn", "infrastructure_error"),
    ],
)
def test_failed_build_result_publication_is_retryable_and_never_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failure_path: str,
    expected_state: str,
) -> None:
    crash = _CrashOnce(boundary)
    executor, request, invocation = _failed_build_case(tmp_path, monkeypatch, failure_path, crash)

    with pytest.raises(_InjectedProcessDeath):
        SimulationRecord(executor, publication_checkpoint=crash).run(request)

    store = SimulationRecordStore(invocation / "targets" / "sim" / "record")
    recovery = store.scan()
    committed = boundary == "after:simulation_result"
    assert bool(recovery.complete) is committed
    attempts = sorted(store.root.glob("work-items/*/attempts/*"))
    assert len(attempts) == 1
    result_path = attempts[0] / "private-build" / "build-result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    assert result is None or result["state"] == expected_state
    if boundary.startswith("before:simulation"):
        outcome = SimulationRecord(executor).run(request)
        assert outcome.acceptance_ready is False
        assert store.scan().items[0].attempt_count == 2
    elif boundary.startswith("after:simulation"):
        outcome = SimulationRecord(executor).run(request)
        assert outcome.acceptance_ready is False
        assert store.scan().items[0].attempt_count == 1


def _new_request(tmp_path: Path) -> NewSimulationRecordRunRequest:
    """A fresh Simulation Record run request for the crash fixture manifest."""
    plan = create_simulation_record_plan(_crash_manifest())
    project, _build, _log = _crash_environment(tmp_path)
    invocation = tmp_path / "reports/000001"
    invocation.mkdir(parents=True)
    return NewSimulationRecordRunRequest(
        plan, project, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )


@pytest.mark.parametrize("boundary", ["before:manifest_commit", "after:manifest_commit"])
def test_manifest_notification_is_committed_before_fault_checkpoint(
    tmp_path: Path, boundary: str
) -> None:
    request = _new_request(tmp_path)
    published: list[Path] = []

    def notify(path: Path) -> None:
        assert path.is_file()
        published.append(path)

    simulation_record = SimulationRecord(
        publication_checkpoint=_CrashOnce(boundary), manifest_published=notify
    )
    with pytest.raises(_InjectedProcessDeath):
        simulation_record.publish_new(request)
    assert len(published) == (0 if boundary.startswith("before") else 1)
    if published:
        simulation_record.publish_new(request)
        assert published == [published[0], published[0]]


def test_closed_console_does_not_skip_postcommit_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.flow import SimulateFlow

    flow = SimulateFlow()
    flow._console_publication_requested = True
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr("booley.flows.sim.flow.sys.stderr", stream)
    published: list[Path] = []
    request = _new_request(tmp_path)
    # Private seam: the publisher is the Flow's own closure around the console; the crash
    # checkpoint must still run after it, and no public run crashes at that point.
    simulation_record = SimulationRecord(
        publication_checkpoint=_CrashOnce("after:manifest_commit"),
        manifest_published=flow._manifest_publisher(published),
    )
    with pytest.raises(_InjectedProcessDeath):
        simulation_record.publish_new(request)
    assert len(published) == 1 and published[0].is_file()


def test_baseline_disclosure_uses_exact_format_without_run_reopening_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.flows.sim.flow import SimulateFlow

    request = _new_request(tmp_path)
    baseline = replace(
        request,
        plan=create_simulation_record_plan(_baseline_disclosure_manifest(_crash_manifest())),
    )
    requests = [baseline, request]
    flow = SimulateFlow()
    published: list[Path] = []
    # Private seam: the Flow's own closure, as in the closed-console test above.
    publish = flow._manifest_publisher(published)
    # A fresh Flow retains the path without printing it.
    publish(tmp_path / "manifest.json")
    assert published == [(tmp_path / "manifest.json").resolve()]
    assert capsys.readouterr().err == ""
    published.clear()
    flow._console_publication_requested = True
    simulation_record = SimulationRecord(manifest_published=publish)
    paths = [simulation_record.publish_new(request) for request in requests]
    # Repeated publication notifications retain one recovery path per invocation.
    for request in requests:
        simulation_record.publish_new(request)
    # A new successful publication at the same destination needs no repeated pointer.
    paths[0].unlink()
    simulation_record.publish_new(requests[0])
    assert paths[0].is_file()
    assert len(set(paths)) == 2
    assert paths[0].parents[1].name == "sim%40baseline-abc123"
    assert capsys.readouterr().err == "".join(
        f"Simulation Record manifest: {path.resolve()}\n" for path in paths
    )
    for request in requests:
        # Private seam: no public entry point reopens a published Simulation Record without
        # running it, and that reopening must not announce the manifest again.
        simulation_record._open_simulation_record(request)
    assert capsys.readouterr().err == ""
    assert published == paths


def _baseline_disclosure_manifest(manifest):
    document = json.loads(canonical_json_bytes(manifest.document))
    document.pop("fingerprints")
    document["target"]["role"] = "cycle_count_baseline"
    for item in document["work_items"]:
        item["target"] = document["target"]
        item["role"] = "cycle_count_baseline"
        identity = {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
        fingerprint = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(identity).rstrip(b"\n")).hexdigest()
        )
        item["fingerprint_sha256"] = fingerprint
        item["work_item_id"] = (
            f"item:{item['ordinal']:04d}:{fingerprint.removeprefix('sha256:')[:16]}"
        )
    return finalize_manifest(document)


def _resume_real_hook_crash(tmp_path, invocation, store):
    from tests.flows.sim.simulation_record_test_support import (
        _executor,
        _handle,
        _legacy_disclosures,
    )

    manifest = store.load_manifest()
    node = ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest))
    project = tmp_path / "project"
    validated = ValidatedResumeManifest(node, (), (_handle(project),))
    destination = invocation.parent / "2"
    destination.mkdir()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    resumed = SimulationRecord(
        _executor(tmp_path / "build", counters, disclosures=_legacy_disclosures())
    ).run(
        ResumeSimulationRecordRunRequest(
            validated,
            create_simulation_record_plan(manifest),
            project,
            invocation.parent,
            SimulationRecordPolicy(),
            destination,
            _admission(),
        )
    )
    return resumed, counters


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_process_death_at_actual_hook_publication_recovers_immutable_evidence(
    tmp_path, monkeypatch, access, side
):
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    crash = _CrashOnce(f"{side}:pre_sim_evidence")
    with pytest.raises(_InjectedProcessDeath):
        _successful_pre_sim_record(tmp_path, monkeypatch, access, checkpoint=crash)
    assert crash.tripped
    invocation = tmp_path / "reports/1"
    store = SimulationRecordStore(invocation / "targets/sim/record")
    old = collect_pre_sim_firings(store.manifest_path)
    assert len(old) == int(side == "after")
    assert store.scan().interrupted
    before_bytes = {firing.path: firing.path.read_bytes() for firing in old}
    resumed, counters = _resume_real_hook_crash(tmp_path, invocation, store)
    assert counters["launch"] == 1
    assert len(resumed.current_pre_sim_keys) == 1
    assert len(resumed.pre_sim_firings) == 1 + len(old)
    assert all(path.read_bytes() == raw for path, raw in before_bytes.items())
    assert all(
        firing.document["stdout_tail"] == "hook-out\n" for firing in resumed.pre_sim_firings
    )


def test_result_commit_and_scan_trust_evidence_changed_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booley's own published evidence is trusted on commit and on every scan."""

    def change_published_evidence(boundary: str) -> None:
        # Rewrite a bundle artifact after verification at launch.
        if boundary == "after:execution_evidence":
            for path in case.store.root.glob("build-variants/*/attempts/*/simv"):
                path.chmod(0o600)
                path.write_bytes(b"changed image")

    case = _serial_case(tmp_path / "case", monkeypatch, True, change_published_evidence)
    outcome = case.record.run(case.request)

    assert outcome.complete is True
    assert [item.state for item in case.store.scan().items] == ["complete"]


class _CancelAfterInputs:
    """Publication checkpoint: the forked worker is cancelled once its inputs are staged.

    ``kill`` exits like a SIGKILL. ``sigint`` signals the worker's own process
    and waits, as the CLI does for Ctrl-C while a lane thread works. The test
    process records whether the destination survived into the next attempt.
    """

    def __init__(self, how: str) -> None:
        self.how = how
        self.destination: Path | None = None
        self.test_process = os.getpid()
        self.present_at_restage: list[bool] = []

    def __call__(self, name: str) -> None:
        if os.getpid() == self.test_process:
            if name == "before:runtime_inputs" and self.destination is not None:
                self.present_at_restage.append(self.destination.exists())
            return
        if name != "after:runtime_inputs":
            return
        if self.how == "kill":
            os._exit(_KILLED)
        os.kill(os.getpid(), signal.SIGINT)
        threading.Event().wait(hang_guard_s())


def _interrupted(run) -> None:
    """Run in the worker like the CLI: a Ctrl-C ends the process without unwinding lanes."""
    try:
        run()
    except KeyboardInterrupt:
        os._exit(_KILLED)


@_needs_fork
@pytest.mark.timeout(180)
@pytest.mark.parametrize("how", ["kill", "sigint"])
@pytest.mark.parametrize("route", ["serial", "coverage"])
def test_cancelled_attempt_leaves_no_runtime_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, how: str
) -> None:
    """A run cancelled while its inputs are staged leaves none for the next attempt."""
    checkpoint = _CancelAfterInputs(how)
    if route == "serial":
        case = _serial_case(tmp_path / "case", monkeypatch, True, checkpoint)
        run = lambda: case.record.run(case.request)  # noqa: E731
        project = case.request.project_root
        destination, parent = project / "run/inputs/input.bin", project / "run/inputs"
        resume = lambda: _resume_crashed_simulation_record(  # noqa: E731
            case.record, case.request, case.store, case.manifest, case.plan, "x"
        )
    else:
        record, request, resume_request, _store, _target = _coverage_case(
            tmp_path / "case", checkpoint
        )
        run = lambda: record.run(request)  # noqa: E731
        project = request.project_root
        destination, parent = project / "data/stimulus.bin", project / "data"
        resume = lambda: record.run(resume_request)  # noqa: E731
    checkpoint.destination = destination

    _run_killed(lambda: _interrupted(run), tmp_path / "trace.txt")
    assert destination.is_file()

    assert resume().complete is True
    assert checkpoint.present_at_restage == [False]
    assert not destination.exists()
    assert not parent.exists()
    ledger = resolve_checkout_booley_dir(project) / ".runtime/staged-runtime-inputs"
    assert list(ledger.glob("*.json")) == []
