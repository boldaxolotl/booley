"""Shared builders and fakes for the Simulation Campaign suites."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.build_session import TargetCompileSurface
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
    SimulationPreview,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.record import capacity as campaign_capacity
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.codec import (
    RESULT_SCHEMA,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_simulation_attempt,
    decode_simulation_result,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.record.coordinator import (
    CampaignOutcome,
    CampaignPolicy,
    CampaignRecoveryStatus,
    NewCampaignRunRequest,
    SimulationCampaign,
    WorkExecutionRequest,
)
from booley.flows.sim.record.coverage_execution import (
    CoverageAggregateExecutor,
)
from booley.flows.sim.record.facts import ACCEPTANCE_FACTS_SCHEMA, AcceptanceFacts
from booley.flows.sim.record.flow_planning import plan_coarse_simulation_campaign
from booley.flows.sim.record.model import (
    SimulationCampaignManifest,
    SimulationResult,
    create_simulation_campaign_plan,
)
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    ValidatedTargetBinding,
)
from booley.flows.sim.record.scheduler import (
    ScheduledAttempt,
)
from booley.flows.sim.record.serial_execution import (
    OrdinaryHdlSerialExecutor,
)
from booley.flows.sim.record.store import CampaignStore
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from booley.flows.test_runs import TestRun, build_test_run, discovered_test_run
from booley.runtime.execution_records import (
    ExecutionId,
)
from booley.runtime.job_slots import (
    CLASS_LIGHT,
    CLASS_MEMORY,
    MemoryClaim,
    SlotStore,
    SlotToken,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.flows.sim.test_simulation_record_manifest_codec import (
    _manifest,
    _selection_test_runs,
    _sha,
)


def _recovery_status(tmp_path: Path) -> CampaignRecoveryStatus:
    return CampaignRecoveryStatus(
        tmp_path / "manifest.json",
        "sha256:" + "1" * 64,
        (),
        (),
        (),
    )


@dataclass(frozen=True)
class _RecordedCampaign:
    """A current-schema manifest written under a test root, one work item per Test Run."""

    manifest_path: Path
    manifest: SimulationCampaignManifest

    @property
    def digest(self) -> str:
        """The digest acceptance facts name the recorded manifest by."""
        return manifest_digest(self.manifest)

    def fact(
        self, run: TestRun, *, discovered: str | None = None, **members: object
    ) -> dict[str, object]:
        """*members* as a v2 observation fact of *run*, as the manifest recorded it.

        Sets ``work_item_id`` (the item recording *run*), ``test_run`` and
        ``test_run_fingerprint`` over *members* (plus ``target`` for a *discovered*
        fact, which the report reads to name the discovered Test Run). *discovered* names a Test an
        unfiltered batch of the unnamed *run* found: the fact's ``test`` is that
        name and its Test Run is production ``discovered_test_run``'s. The
        caller's ``test`` member is otherwise kept as given.
        """
        (item,) = (
            item
            for item in cast(Sequence[Mapping[str, Any]], self.manifest.document["work_items"])
            if any(doc["name"] == run.name for doc in item["test_runs"])
        )
        selector = cast(Mapping[str, Any], item["target"])["selector"]
        observed = (
            run if discovered is None else discovered_test_run(discovered, run, target=selector)
        )
        stamped: dict[str, object] = {
            "work_item_id": item["work_item_id"],
            "test_run": observed.name,
            "test_run_fingerprint": observed.fingerprint,
        }
        if discovered is not None:
            stamped["test"] = discovered
            stamped["target"] = item["target"]
        return members | stamped


def _recorded_campaign(
    root: Path, runs: Sequence[TestRun], required: tuple[str, ...] = ("reset", "count")
) -> _RecordedCampaign:
    """Plan *runs* with the real planner and write the manifest as ``manifest.json``.

    Each call plans in a fresh directory under *root*: the planner's fixture
    creates ``.booley/`` and sources there, so one root cannot plan twice and
    tests that use *root* as a work directory stay free of them.
    """
    from tests.flows.sim.test_simulation_record_test_runs import _hdl_plan

    directory = Path(tempfile.mkdtemp(prefix="recorded-campaign-", dir=root))
    manifest = _hdl_plan(directory, tuple((run,) for run in runs), required).manifest
    manifest_path = directory / "manifest.json"
    manifest_path.write_bytes(encode_simulation_campaign_manifest(manifest))
    return _RecordedCampaign(manifest_path, manifest)


def _acceptance_facts(manifest_sha256: str) -> dict[str, object]:
    return {
        "$schema": ACCEPTANCE_FACTS_SCHEMA,
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": manifest_sha256,
        "origin": {"execution_id": "", "invocation_id": 1},
        "target": {
            "vlnv": "acme:lib:dut:1",
            "name": "sim",
            "selector": "sim",
            "project_identity": "project",
            "revision": "abc",
            "role": "candidate",
            "display_name": "sim",
        },
        "required_suite": {
            "names": ["smoke"],
            "default_invocation": False,
            "source_sha256": "sha256:" + "2" * 64,
        },
        "prerequisites": [],
        "consumed_results": [],
        "observations": [],
        "coverage_reference": None,
    }


def _observation(test: str | None) -> dict[str, object]:
    return {
        "work_item_id": "item:0000:0123456789abcdef",
        "role": "candidate",
        "revision": "abc",
        "target": _acceptance_facts("")["target"],
        "result_sha256": "sha256:" + "3" * 64,
        "test": test,
        "execution": "completed",
        "failure_class": None,
        "functional": "pass",
        "assertions": "clean",
        "assertion_count": 0,
        "detail": {},
        "cycle_count": None,
    }


def _simulation_outcome(
    tmp_path: Path,
    observations: list[dict[str, object]],
    *,
    required: tuple[str, ...] = ("half", "full"),
    complete: bool = True,
    role: str = "candidate",
    seeds: Mapping[str, int] | None = None,
) -> CampaignOutcome:
    """A Campaign outcome of *observations*; *seeds* names the seed of a test's Test Run."""
    runs: dict[str | None, TestRun] = {}
    for observation in observations:
        test = cast("str | None", observation["test"])
        seed = None if test is None or seeds is None else seeds.get(test)
        runs.setdefault(test, build_test_run(test, seed=seed))
    recorded = _recorded_campaign(tmp_path, tuple(runs.values()), required)
    document = _acceptance_facts(recorded.digest)
    target = dict(document["target"])  # type: ignore[arg-type]
    target["role"] = role
    document["target"] = target
    document["required_suite"] = {
        "names": list(required),
        "default_invocation": not required,
        "source_sha256": "sha256:" + "2" * 64,
    }
    normalized = []
    for observation in observations:
        run = runs[cast("str | None", observation["test"])]
        normalized.append(
            recorded.fact(run, **cast("dict[str, Any]", observation))
            | {"target": target, "role": role}
        )
    document["observations"] = normalized
    facts = AcceptanceFacts(document)
    passing = all(
        item["execution"] == "completed"
        and item["functional"] == "pass"
        and item["assertions"] != "dirty"
        for item in normalized
    )
    selected = {item["test"] for item in normalized}
    acceptance_ready = (
        complete
        and passing
        and (
            len(normalized) == 1 and None in selected
            if not required
            else set(required).issubset(selected)
        )
    )
    return CampaignOutcome(
        recorded.manifest_path,
        tmp_path / "summary.json",
        facts.document["target"],  # type: ignore[arg-type]
        tuple(facts.document["observations"]),  # type: ignore[arg-type]
        "pass" if passing else "fail",
        complete,
        None,
        facts,
        acceptance_ready,
        _recovery_status(tmp_path),
    )


def _admission() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False)


def _build_execution() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-build-execution/v1",
        "process": {
            "returncode": 0,
            "stdout": "compile\n",
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
            "output": "compile\n",
            "returncode": 0,
            "timed_out": False,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
            "terminal_record": True,
            "reason": "",
            "cache_decision": "",
        },
    }


def _two_item_manifest() -> object:
    return _named_manifest(("alpha", "beta"))


def _named_manifest(names: tuple[str, ...]) -> object:
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": "runs/{test}/{attempt}",
        "kind": "templated",
        "placeholders": ["test", "attempt"],
    }
    runtime_identity = {
        "source_artifact_path": "input.bin",
        "destination": "inputs/input.bin",
    }
    document["workload"]["runtime_inputs"] = [  # type: ignore[index]
        {
            "declaration_id": "sha256:"
            + hashlib.sha256(canonical_json_bytes(runtime_identity).rstrip(b"\n")).hexdigest(),
            **runtime_identity,
        }
    ]
    suite_raw = "".join(f"[test.{name}]\n" for name in names).encode()
    document["required_suite"] = {
        "names": list(names),
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    target = document["target"]
    variant = document["build_variants"][0]  # type: ignore[index]
    document["work_items"] = [
        _work_item(ordinal, name, target, variant["build_variant_id"])
        for ordinal, name in enumerate(names)
    ]
    return finalize_manifest(document)


def _unfiltered_cocotb_manifest() -> object:
    document = _manifest()
    document.pop("fingerprints")
    item = document["work_items"][0]  # type: ignore[index]
    item_identity = {
        key: value
        for key, value in item.items()
        if key not in {"work_item_id", "fingerprint_sha256"}
    }
    item_identity["kind"] = "cocotb_batch"
    item_identity["selection"] = {"kind": "unfiltered", "names": []}
    item_identity["arguments"] = []
    fingerprint = _sha(item_identity)
    document["work_items"] = [
        {
            "work_item_id": "item:0000:" + fingerprint.removeprefix("sha256:")[:16],
            **item_identity,
            "fingerprint_sha256": fingerprint,
        }
    ]
    return finalize_manifest(document)


def _single_item_selection_manifest(kind: str, names: tuple[str, ...]) -> object:
    document = _manifest()
    document.pop("fingerprints")
    item = document["work_items"][0]  # type: ignore[index]
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"work_item_id", "fingerprint_sha256"}
    }
    identity["selection"] = {"kind": kind, "names": list(names)}
    identity["test_runs"] = _selection_test_runs(names)
    identity["arguments"] = list(names)
    if len(names) > 1:
        identity["kind"] = "cocotb_batch"
    fingerprint = _sha(identity)
    document["work_items"] = [
        {
            "work_item_id": "item:0000:" + fingerprint.removeprefix("sha256:")[:16],
            **identity,
            "fingerprint_sha256": fingerprint,
        }
    ]
    return finalize_manifest(document)


def _work_item(ordinal: int, name: str, target: object, variant_id: object) -> dict:
    identity = {
        "ordinal": ordinal,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": target,
        "selection": {"kind": "named", "names": [name]},
        "test_runs": _selection_test_runs((name,)),
        "arguments": [name],
        "build_variant_id": variant_id,
        "run_directory": {
            "configured": "runs/{test}/{attempt}",
            "kind": "templated",
            "collision_template": "runs/test/attempt",
        },
    }
    fingerprint = _sha(identity)
    return {
        "work_item_id": f"item:{ordinal:04d}:" + fingerprint.removeprefix("sha256:")[:16],
        **identity,
        "fingerprint_sha256": fingerprint,
    }


def _handle(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )


def _failed_shared_executor(
    build_root: Path,
    counters: dict[str, int],
    on_compile: Callable[[], None] | None = None,
    *,
    infrastructure: bool = False,
) -> OrdinaryHdlSerialExecutor:
    build_root.mkdir()

    class FailedGroup:
        artifact_paths: tuple[Path, ...] = ()

        def __init__(self) -> None:
            self.build_root = build_root

        def planning_disclosure(self):
            return {}

        def compile(self):
            counters["compile"] += 1
            if on_compile is not None:
                on_compile()
            return SimpleNamespace(passed=False)

        def finish_build_failure(self):
            outcome = _failed_shared_outcome()
            if not infrastructure:
                return outcome
            return replace(
                outcome,
                infrastructure_failure=SimulationInfrastructureFailure(
                    "spawn", "the compiler was killed"
                ),
            )

        def launch_snapshot(self, *_args, **_kwargs):
            counters["launch"] += 1
            raise AssertionError("failed shared build must not launch")

    class FailedExecution:
        @contextmanager
        def ordinary_group(self, _handle, _names, _test_runs=None):
            yield FailedGroup()

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FailedExecution(),  # type: ignore[arg-type,return-value]
    )


def _failed_shared_outcome() -> SimulationTargetOutcome:
    test = SimulationTestOutcome(
        name="tail",
        verdict="elab_error",
        passed=False,
        reason="compiler reason",
        error_tail="compiler tail",
        elab_failed=True,
    )
    return SimulationTargetOutcome(
        target="sim",
        target_identity="acme:lib:dut:1#sim",
        toplevel="tb",
        eda_tool="icarus",
        passed=False,
        verdict="fail",
        elapsed_s=0.1,
        tests=(test,),
    )


def _completed_result_documents(store: CampaignStore) -> list[dict[str, object]]:
    return [
        json.loads((store.work_item_directory(item) / "result.json").read_text())
        for item in store.scan().complete
    ]


def _shared_executor(build_root, run_log, handle, launches, compile_count, bindings):
    class FakeGroup:
        def __init__(self, names: tuple[str, ...]) -> None:
            self.names = names
            self.build_root = build_root
            self.artifact_paths = (build_root / "simv",)
            self.compile_surface = SimpleNamespace(
                project_root=build_root.parent.resolve(),
                authored_paths=(),
                operational_paths=(),
                optional_paths=(),
            )
            self.reused = False

        def planning_disclosure(self):
            return {}

        def compile(self):
            compile_count[0] += 1
            return SimpleNamespace(passed=True)

        def build_recovery_document(self):
            return _build_execution()

        def bind_authenticated_bundle(self, evidence) -> None:
            assert evidence == _build_execution()
            bindings.append(self.names)

        def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
            name = self.names[0]
            staged = run_cwd / "inputs/input.bin"
            launches.append((name, run_cwd, staged.read_bytes()))
            staged.write_bytes(name.encode())
            assert (snapshot_root / "simv").read_bytes() == b"image"
            return _shared_outcome(name, handle, run_log)

    class FakeExecution:
        @contextmanager
        def ordinary_group(self, _handle, names, _test_runs=None):
            yield FakeGroup(names)

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
    )


def _shared_outcome(name, handle, run_log):
    test = SimulationTestOutcome(
        name=name, verdict="pass", passed=True, cycles=1234, run_log_path=str(run_log)
    )
    return SimulationTargetOutcome(
        target="sim",
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="icarus",
        passed=True,
        verdict="pass",
        elapsed_s=0.1,
        tests=(test,),
    )


_BAD_DIGEST = "sha256:" + "f" * 64


@dataclass(frozen=True)
class _Completed:
    store: CampaignStore
    item_id: str
    attempt_dir: Path
    build_dir: Path


class _TestGroup:
    def __init__(self, build_root: Path, name: str) -> None:
        self.build_root = build_root
        self.name = name
        self.artifact_paths = (build_root / "simv", build_root / "helper.so")
        self.compile_surface = SimpleNamespace(
            project_root=build_root.parent.resolve(),
            authored_paths=(),
            operational_paths=(),
            optional_paths=(),
        )

    def planning_disclosure(self) -> dict[str, object]:
        return {}

    def compile(self) -> SimpleNamespace:
        return SimpleNamespace(passed=True)

    def build_recovery_document(self) -> dict[str, object]:
        return {
            "$schema": "booley.simulation-build-execution/v1",
            "process": {
                "returncode": 0,
                "stdout": "",
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
                "output": "",
                "returncode": 0,
                "timed_out": False,
                "peak_rss_mb": None,
                "oom_kill_delta": 0,
                "terminal_record": True,
                "reason": "",
                "cache_decision": "",
            },
        }

    def bind_authenticated_bundle(self, _evidence: object) -> None:
        return None

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path) -> SimulationTargetOutcome:
        assert (snapshot_root / "simv").is_file()
        assert run_cwd.is_dir()
        test = SimulationTestOutcome(name=self.name, verdict="pass", passed=True)
        artifacts = tuple(
            SimulationArtifactEvidence(kind, str(self.build_root / name), 4, (self.name,))
            for kind, name in (("run_log", "run.log"), ("trace", "trace.fst"))
        )
        return SimulationTargetOutcome(
            target="sim",
            target_identity="acme:lib:dut:1#sim",
            toplevel="tb",
            eda_tool="icarus",
            passed=True,
            verdict="pass",
            elapsed_s=0.01,
            tests=(test,),
            artifacts=artifacts,
        )


class _TestExecution:
    def __init__(self, build_root: Path) -> None:
        self._build_root = build_root

    @contextmanager
    def ordinary_group(self, _handle: object, names: tuple[str, ...], _test_runs=None):
        yield _TestGroup(self._build_root, names[0])


def _recovery_executor(build_root: Path) -> OrdinaryHdlSerialExecutor:
    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _TestExecution(build_root),  # type: ignore[arg-type,return-value]
    )


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.chmod(0o700)
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(document))


def _create_build_root(root: Path) -> Path:
    root.mkdir()
    (root / "simv").write_bytes(b"image")
    (root / "helper.so").write_bytes(b"library")
    (root / "run.log").write_bytes(b"log\n")
    (root / "trace.fst").write_bytes(b"fst\n")
    return root


def _completed(tmp_path: Path) -> _Completed:
    manifest = _manifest_for(("alpha",))
    project = tmp_path / "project"
    project.mkdir()
    build_root = _create_build_root(tmp_path / "engine-build")
    store = CampaignStore(tmp_path / "record")
    store.publish_manifest(manifest)
    item = manifest.document["work_items"][0]
    request = _request(store, manifest, item, project, invocation=1)
    result = _recovery_executor(build_root).execute(request)
    store.publish_result(item["work_item_id"], result)
    assert store.scan().complete == (item["work_item_id"],)
    build_dir = next(store.root.glob("build-variants/*/attempts/*"))
    return _Completed(store, item["work_item_id"], request.attempt_directory, build_dir)


def _run_identity() -> dict[str, str]:
    return {
        "simulation_record_id": str(uuid.uuid4()),
        "work_item_id": "item:0000:" + "a" * 16,
        "attempt_id": str(uuid.uuid4()),
    }


def _manifest_for(names: tuple[str, ...], *, access: str = "immutable"):
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = access  # type: ignore[index]
    document["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": "runs/{test}/{attempt}",
        "kind": "templated",
        "placeholders": ["test", "attempt"],
    }
    suite_raw = "".join(f"[test.{name}]\n" for name in names).encode()
    document["required_suite"] = {
        "names": list(names),
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    target = document["target"]
    variant = document["build_variants"][0]  # type: ignore[index]
    # Planning writes this flag as ``pre_sim_build_access == "immutable"``.
    variant["sharing_eligible"] = access == "immutable"
    items = []
    for ordinal, name in enumerate(names):
        identity = {
            "ordinal": ordinal,
            "kind": "ordinary_hdl",
            "role": "candidate",
            "revision": "abc123",
            "target": target,
            "selection": {"kind": "named", "names": [name]},
            "test_runs": _selection_test_runs((name,)),
            "arguments": [name],
            "build_variant_id": variant["build_variant_id"],
            "run_directory": {
                "configured": "runs/{test}/{attempt}",
                "kind": "templated",
                "collision_template": "runs/test/attempt",
            },
        }
        fingerprint = _sha(identity)
        items.append(
            {
                "work_item_id": f"item:{ordinal:04d}:" + fingerprint.removeprefix("sha256:")[:16],
                **identity,
                "fingerprint_sha256": fingerprint,
            }
        )
    document["work_items"] = items
    return finalize_manifest(document)


class _Group:
    def __init__(
        self,
        names: tuple[str, ...],
        build_root: Path,
        counters: dict[str, int],
        *,
        mutate_snapshot: bool = False,
        disclosure: object | None = None,
    ) -> None:
        self.names = names
        self.build_root = build_root
        self.compile_surface = TargetCompileSurface(build_root.parent, (), ())
        self.artifact_paths = (build_root / "simv",)
        self._counters = counters
        self._mutate_snapshot = mutate_snapshot
        self._disclosure = disclosure or {}

    def planning_disclosure(self):
        return self._disclosure

    def compile(self):
        self._counters["compile"] += 1
        return SimpleNamespace(passed=True)

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()
        self._counters["durable_reuse"] += 1

    def launch_snapshot(self, snapshot_root: Path, _run_cwd: Path):
        self._counters["launch"] += 1
        if self._mutate_snapshot:
            executable = snapshot_root / "simv"
            executable.chmod(0o700)
            executable.write_bytes(b"mutated")
        name = self.names[0]
        test = SimulationTestOutcome(name=name, verdict="pass", passed=True)
        return SimulationTargetOutcome(
            target="sim",
            target_identity="acme:lib:dut:1#sim",
            toplevel="tb",
            eda_tool="icarus",
            passed=True,
            verdict="pass",
            elapsed_s=0.01,
            tests=(test,),
        )


class _Execution:
    def __init__(
        self,
        build_root: Path,
        counters: dict[str, int],
        *,
        mutate_snapshot: bool = False,
        disclosures: dict[str, object] | None = None,
    ) -> None:
        self._build_root = build_root
        self._counters = counters
        self._mutate_snapshot = mutate_snapshot
        self._disclosures = disclosures or {}

    @contextmanager
    def ordinary_group(self, _handle: object, names: tuple[str, ...], _test_runs=None):
        yield _Group(
            names,
            self._build_root,
            self._counters,
            mutate_snapshot=self._mutate_snapshot,
            disclosure=self._disclosures.get(names[0]),
        )


def _executor(
    build_root: Path,
    counters: dict[str, int],
    *,
    mutate_snapshot: bool = False,
    disclosures: dict[str, object] | None = None,
    checkpoint=None,
) -> OrdinaryHdlSerialExecutor:
    return OrdinaryHdlSerialExecutor(
        publication_checkpoint=checkpoint,
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _Execution(
            build_root,
            counters,
            mutate_snapshot=mutate_snapshot,
            disclosures=disclosures,
        ),  # type: ignore[arg-type,return-value]
    )


def _request(
    store: CampaignStore,
    manifest,
    item: object,
    project: Path,
    *,
    invocation: int,
    policy: CampaignPolicy | None = None,
) -> WorkExecutionRequest:
    item_id = item["work_item_id"]  # type: ignore[index]
    attempt_id = str(uuid.uuid4())
    ordinal, directory = store.allocate_attempt_directory(item_id, attempt_id)
    return WorkExecutionRequest(
        store,
        manifest,
        item,  # type: ignore[arg-type]
        attempt_id,
        ordinal,
        directory,
        invocation,
        policy or CampaignPolicy(),
        _admission(),
        project,
        _handle(project),  # type: ignore[arg-type]
    )


def _legacy_disclosures():
    return {
        name: {
            "planner": f"fake-{name}",
            "scratch_inputs": [],
            "generated_files": [],
            "tool_provenance": {"kind": "fake", "version": "1", "contract_version": "1"},
            "cleanup": {"removed": True},
        }
        for name in ("alpha", "beta")
    }


def _maximum_hook_selection_manifest(names):
    from tests.flows.sim.test_endpoint_record_lifecycle import _pre_sim_manifest

    def transform(document):
        workload = document["workload"]
        workload["coverage"] = True
        item = document["work_items"][0]
        item.update(
            kind="coverage_aggregate",
            selection={"kind": "named", "names": names},
            test_runs=_selection_test_runs(names),
            arguments=list(names),
        )
        suite = document["required_suite"]
        source = "\n".join(names).encode()
        suite.update(
            names=list(names),
            source_bytes=len(source),
            source_sha256="sha256:" + hashlib.sha256(source).hexdigest(),
        )

    return _pre_sim_manifest("immutable", ("true",), transform)


def _maximum_hook_selection_plan(names):
    from booley.flows.sim.record.model import create_simulation_campaign_plan

    manifest, disclosures = _maximum_hook_selection_manifest(names)
    return create_simulation_campaign_plan(manifest), disclosures


def _hook_publication_request(root, names=("alpha", "beta"), invocation=1):
    plan, _ = _maximum_hook_selection_plan(names)
    store = CampaignStore(root / "record")
    store.publish_manifest(plan.manifest)
    request = _request(
        store,
        plan.manifest,
        plan.manifest.document["work_items"][0],
        root,
        invocation=invocation,
    )
    _executor(root / "build", {}).prepare_attempt(request)
    return request


def _publish_request_hook(request, name="alpha", ordinal=1, status="passed", returncode=0):
    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.record.pre_sim_evidence import publish_pre_sim_firing

    return publish_pre_sim_firing(
        request,
        PreSimEvidence(("true",), (name,), status, 0.0, returncode=returncode),
        ordinal=ordinal,
    )


def _observe_pre_sim_reads(monkeypatch):
    from collections import Counter

    from booley.flows.sim.record.pre_sim_evidence import PreSimFiringDecoder

    accesses, decodes, results = Counter(), Counter(), Counter()

    def observe(method):
        def read(store, path, **kwargs):
            if path.parent.name == "pre-sim":
                accesses[path] += 1
            return method(store, path, **kwargs)

        return read

    read_bytes = CampaignStore._read

    def count_results(store, path, **kwargs):
        if path.name == "result.json":
            results[path] += 1
        return read_bytes(store, path, **kwargs)

    decode = PreSimFiringDecoder.decode

    def count_decode(decoder, raw, item, attempt):
        document = decode(decoder, raw, item, attempt)
        decodes[(document["attempt_id"], document["ordinal"])] += 1
        return document

    monkeypatch.setattr(CampaignStore, "_read", observe(count_results))
    monkeypatch.setattr(CampaignStore, "_identity", observe(CampaignStore._identity))
    monkeypatch.setattr(PreSimFiringDecoder, "decode", count_decode)
    return accesses, decodes, results


_BUDGET_MIB = 1000.0


def _holder_claim(holders: int) -> MemoryClaim:
    """A claim sized so that exactly *holders* of them fit the 1,000 MiB budget at once."""
    return MemoryClaim(budget_mb=_BUDGET_MIB, reservation_mb=_BUDGET_MIB / (holders + 0.5))


def _outer_claim(store: SlotStore) -> SlotToken:
    """The Flow's own claim, as an admitted Campaign holds it once its items take their own."""
    return store.acquire(
        CLASS_MEMORY,
        pid=os.getpid(),
        execution_id=ExecutionId("a" * 32),
        memory=MemoryClaim(budget_mb=_BUDGET_MIB, reservation_mb=1.0),
    )


class _FixedSizing:
    """Stands in for ``MemorySizing``: every work item claims the same reservation."""

    def __init__(self, claim: MemoryClaim) -> None:
        self.claim = claim
        self.warnings: list[str] = []

    def plan_work_item(self, _request: object, *, build: bool) -> MemoryClaim:
        return self.claim


def _managed(store: SlotStore, outer, *, holders: int = 2, **overrides: Any) -> AdmissionContext:
    """A managed admission whose Campaign lends *holders* claims' worth of memory."""
    return AdmissionContext(
        "managed",
        store,
        outer,
        "interactive",
        "a" * 32,
        5.0,
        lambda: False,
        memory_sizing=_FixedSizing(_holder_claim(holders)),  # type: ignore[arg-type]
        **overrides,
    )


def _pinned_lanes(*args: Any, lanes: int, **kwargs: Any) -> campaign_capacity.CampaignCapacity:
    """Real capacity whose lane bound a test pins, not the machine's cores."""

    class _PinnedLanes(campaign_capacity.CampaignCapacity):
        @property
        def max_lanes(self) -> int:
            return lanes

    return _PinnedLanes(*args, **kwargs)


def _item_capacity(
    admission: AdmissionContext, manifest, project_data: Path, *, lanes: int = 2
) -> campaign_capacity.CampaignCapacity:
    """Capacity whose every work item takes the admission's claim for itself."""
    sizing = admission.memory_sizing
    claim = sizing.claim if isinstance(sizing, _FixedSizing) else _holder_claim(lanes)
    return _pinned_lanes(admission, manifest, project_data, lambda _item: claim, lanes=lanes)


def _capacity(
    admission: AdmissionContext, tmp_path: Path, *, lanes: int = 2
) -> campaign_capacity.CampaignCapacity:
    """Real capacity, with its lane registry, over a fresh Booley Directory."""
    project_data = tmp_path / "project" / ".booley"
    project_data.mkdir(parents=True, exist_ok=True)
    if admission.memory_sizing is None:  # a Campaign that sizes no item claims
        return campaign_capacity.CampaignCapacity(admission, _two_item_manifest(), project_data)
    return _item_capacity(admission, _two_item_manifest(), project_data, lanes=lanes)


def _lane_registry(tmp_path: Path) -> ChildExecutionRegistry:
    """A registry over the Booley Directory ``_capacity`` uses, to read lane records."""
    project_data = tmp_path / "project" / ".booley"
    return ChildExecutionRegistry.from_project_data(
        _two_item_manifest(), project_data, booley_dir=project_data
    )


def _three_item_manifest(execution_id: str = ""):
    document = json.loads(canonical_json_bytes(_two_item_manifest().document))
    document.pop("fingerprints")
    document["origin"]["execution_id"] = execution_id
    document["required_suite"]["names"] = ["alpha", "beta", "gamma"]
    identity = {
        key: value
        for key, value in document["work_items"][1].items()
        if key not in {"work_item_id", "fingerprint_sha256"}
    }
    identity["ordinal"] = 2
    identity["selection"] = {"kind": "named", "names": ["gamma"]}
    from tests.flows.sim.test_simulation_record_manifest_codec import _selection_test_runs

    identity["test_runs"] = _selection_test_runs(("gamma",))
    identity["arguments"] = ["gamma"]
    fingerprint = _sha(identity)
    document["work_items"].append(
        {
            "work_item_id": "item:0002:" + fingerprint.removeprefix("sha256:")[:16],
            **identity,
            "fingerprint_sha256": fingerprint,
        }
    )
    return finalize_manifest(document)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before the deadline")


_PARALLEL_SYNC_TIMEOUT_S = 20.0


def _scheduled_attempt(store: CampaignStore, item) -> ScheduledAttempt:
    attempt_id = os.urandom(16).hex()
    ordinal, directory = store.allocate_attempt_directory(str(item["work_item_id"]), attempt_id)
    return ScheduledAttempt(item, attempt_id, ordinal, directory, attempt_id)


class _ParallelState:
    def __init__(self, build_root, run_log, handle, first_finisher, schedule_seed):
        self.build_root = build_root
        self.run_log = run_log
        self.handle = handle
        self.first_finisher = first_finisher
        self.schedule_seed = schedule_seed
        self.barrier = threading.Barrier(2, timeout=_PARALLEL_SYNC_TIMEOUT_S)
        self.first_finished = threading.Event()
        self.interval_gate = threading.Lock()
        self.intervals: list[tuple[str, float, float]] = []
        self.completion_order: list[str] = []
        self.compile_count = 0


class _ParallelGroup:
    def __init__(self, state: _ParallelState, names: tuple[str, ...]) -> None:
        self.state = state
        self.names = names
        self.build_root = state.build_root
        self.artifact_paths = (state.build_root / "simv",)
        self.compile_surface = SimpleNamespace(
            project_root=state.build_root.parent.resolve(),
            authored_paths=(),
            operational_paths=(),
            optional_paths=(),
        )

    def planning_disclosure(self):
        return {}

    def compile(self):
        self.state.compile_count += 1
        return SimpleNamespace(passed=True)

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
        del snapshot_root, run_cwd
        name = self.names[0]
        started = time.monotonic()
        self._coordinate(name)
        finished = time.monotonic()
        with self.state.interval_gate:
            self.state.intervals.append((name, started, finished))
            self.state.completion_order.append(name)
        passed = name != "beta"
        test = SimulationTestOutcome(
            name, "pass" if passed else "fail", passed, str(self.state.run_log)
        )
        return SimulationTargetOutcome(
            "sim",
            self.state.handle.identity,
            "tb",
            "icarus",
            passed,
            "pass" if passed else "fail",
            0.1,
            (test,),
        )

    def _coordinate(self, name: str) -> None:
        # The first item builds alone under the Flow's claim; the next two overlap.
        if name not in {"beta", "gamma"}:
            return
        self.state.barrier.wait()
        time.sleep(0.001 * self.state.schedule_seed)
        if name == self.state.first_finisher:
            self.state.first_finished.set()
        else:
            assert self.state.first_finished.wait(timeout=_PARALLEL_SYNC_TIMEOUT_S)


class _ParallelExecution:
    def __init__(self, state: _ParallelState) -> None:
        self.state = state

    @contextmanager
    def ordinary_group(self, _handle, names, _test_runs=None):
        yield _ParallelGroup(self.state, names)


class _CampaignAdmissionEndpoint:
    _invocation_id = "sim-20260925T153819Z-1"

    def __init__(self, slots: SlotStore, runtime_execution_id: str | None) -> None:
        self._slots = slots
        self._runtime_execution_id = runtime_execution_id

    def _memory_admitted(self) -> bool:
        return False

    def _acquire_job_slot(self):
        execution_id = (
            ExecutionId(self._runtime_execution_id) if self._runtime_execution_id else None
        )
        return self._slots, self._slots.acquire(
            CLASS_LIGHT, pid=os.getpid(), execution_id=execution_id
        )


def _validated_campaign(store, manifest, project, handle) -> ValidatedResumeManifest:
    node = ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest))
    return ValidatedResumeManifest(
        node,
        (),
        (handle,),
        (ValidatedTargetBinding(node, project.resolve(), handle),),
    )


def _interrupted_campaign(tmp_path, monkeypatch, runtime_execution_id):
    manifest = _three_item_manifest(runtime_execution_id or "")
    plan = create_simulation_campaign_plan(manifest)
    project, state = _parallel_environment(tmp_path, "beta", 0)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _ParallelExecution(state),  # type: ignore[arg-type,return-value]
    )
    invocation = tmp_path / "reports/000001"
    invocation.mkdir(parents=True)
    slots = SlotStore(tmp_path / "slots")
    _set_runtime_execution_id(monkeypatch, runtime_execution_id)
    monkeypatch.setenv("BOOLEY_RUN_ID", _CampaignAdmissionEndpoint._invocation_id)
    fault_enabled, original_mark = [True], ChildExecutionRegistry.mark_terminal
    work_items = invocation / "targets/sim/record/work-items"

    def interrupted_mark(registry, prepared, terminal_cause) -> None:
        if fault_enabled[0]:
            # Interrupt only after every item has published its result.
            _wait_until(
                lambda: len(tuple(work_items.glob("*/result.json"))) == 3,
                timeout=_PARALLEL_SYNC_TIMEOUT_S,
            )
            raise RuntimeError("injected terminal-record interruption")
        original_mark(registry, prepared, terminal_cause)

    monkeypatch.setattr(ChildExecutionRegistry, "mark_terminal", interrupted_mark)
    campaign = SimulationCampaign(executor, work_item_request=lambda *_args: None)
    execution_id = ExecutionId(runtime_execution_id) if runtime_execution_id else None
    # The Flow's claim, with three item claims' worth of memory left to lend its Campaign.
    outer = slots.acquire(
        CLASS_MEMORY,
        pid=os.getpid(),
        execution_id=execution_id,
        memory=MemoryClaim(budget_mb=_BUDGET_MIB, reservation_mb=1.0),
    )
    admission = replace(_managed(slots, outer, holders=3), execution_id=execution_id)
    try:
        with pytest.raises(RuntimeError, match="injected terminal-record interruption"):
            campaign.run(
                NewCampaignRunRequest(
                    plan, project, invocation.parent, CampaignPolicy(), invocation, admission
                )
            )
    finally:
        slots.release(outer)
    store = CampaignStore(invocation / "targets/sim/record")
    validated = _validated_campaign(store, manifest, project, state.handle)
    return SimpleNamespace(
        campaign=campaign,
        fault_enabled=fault_enabled,
        invocation=invocation,
        manifest=manifest,
        plan=plan,
        project=project,
        slots=slots,
        state=state,
        store=store,
        validated=validated,
    )


def _set_runtime_execution_id(
    monkeypatch: pytest.MonkeyPatch, runtime_execution_id: str | None
) -> None:
    if runtime_execution_id is None:
        monkeypatch.delenv("BOOLEY_RUNTIME_EXECUTION_ID", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", runtime_execution_id)


def _parallel_environment(tmp_path, first_finisher, schedule_seed):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"runtime")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )
    return project, _ParallelState(build_root, run_log, handle, first_finisher, schedule_seed)


def _facts(
    root: Path,
    *,
    cocotb: bool,
    preview_names: tuple[str, ...] = ("count", "reset"),
    access: str = "immutable",
) -> tuple[TargetHandle, TargetInspection, SimulationPreview]:
    project = root / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(
        f"[flows.sim]\nrun_cwd = '.'\npre_sim_build_access = '{access}'\n", encoding="utf-8"
    )
    (project / "tests.toml").write_text('[sim]\ntests = ["reset", "count"]\n', encoding="utf-8")
    source = root / ("test_counter.py" if cocotb else "tb.sv")
    source.write_text("# cocotb\n" if cocotb else "module tb; endmodule\n", encoding="utf-8")
    if cocotb:
        _write_cocotb_core(root)
    handle = cast(
        TargetHandle,
        SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            selector="sim",
            name="sim",
            vlnv="acme:lib:dut:1",
            project_root=root.resolve(),
        ),
    )
    flow_options = {"cocotb_module": "test_counter"} if cocotb else {}
    inputs = (
        TargetInput(
            source.name,
            "acme:lib:dut:1",
            "user" if cocotb else "systemVerilogSource",
            ("tb",),
            False,
            {},
        ),
    )
    inspection = TargetInspection(handle, "tb", "sim", "verilator", flow_options, {}, inputs)
    preview = SimulationPreview(
        commands=(("run-batch", *preview_names),),
        groups=(preview_names,),
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="verilator",
        sources=(source.name,),
        constraints=(),
        parameters={},
        flow_options=flow_options,
    )
    return handle, inspection, preview


def _write_cocotb_core(root: Path) -> None:
    (root / "dut.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:dut:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - test_counter.py: {file_type: user, copyto: test_counter.py}\n"
        "targets:\n"
        "  sim:\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n"
        "    flow: sim\n"
        "    flow_options:\n"
        "      tool: icarus\n"
        "      cocotb_module: test_counter\n",
        encoding="utf-8",
    )


def _plan(
    root: Path,
    *,
    kind: str,
    names: tuple[str, ...] = ("count", "reset"),
    cocotb: bool,
    no_waivers: bool = False,
    access: str = "immutable",
):
    handle, inspection, preview = _facts(root, cocotb=cocotb, preview_names=names, access=access)
    return plan_coarse_simulation_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=names,
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=7,
        execution_id="7" * 32,
        trace=False,
        kind=kind,
        no_waivers=no_waivers,
    )


class _FailedCoverageBuild(NativeExecution):
    def __init__(self, *, infrastructure: bool, reason: str = "compile failed") -> None:
        super().__init__()
        self.infrastructure = infrastructure
        self.reason = reason

    def build(self, request):
        del request
        return SimulationBuildResult(False, self.reason, infrastructure_error=self.infrastructure)

    def run(self, request):
        raise AssertionError(f"failed build launched {request.run_id}")


def _coverage_runtime_input(root: Path) -> None:
    (root / "stimulus.bin").write_bytes(b"source runtime input")
    core = root / "counter.core"
    core.write_text(
        core.read_text().replace(
            "files: [rtl/counter.sv]",
            "files: [rtl/counter.sv, {stimulus.bin: {file_type: user, "
            "copyto: data/stimulus.bin}}]",
        )
    )


def _coverage_campaign_plan(
    root: Path, *, runtime_input: bool = False, pre_sim_commands: tuple[str, ...] = ()
):
    context = project(root)
    if runtime_input:
        _coverage_runtime_input(root)
    project_data = root / ".booley"
    project_data.mkdir()
    (project_data / "booley.toml").write_text(
        "[flows.sim]\npre_run_commands = " + json.dumps(pre_sim_commands) + "\n",
        encoding="utf-8",
    )
    (project_data / "tests.toml").write_text(
        '[sim_0]\ntests = ["reset", "wrap"]\n', encoding="utf-8"
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    target = prepared.plan.targets[0]
    inspection = TargetCatalog.build(root).inspect(target.handle)
    names = target.selected_tests
    preview = SimulationPreview(
        commands=(("coverage", *names),),
        groups=(names,),
        target_identity=target.handle.identity,
        toplevel=inspection.toplevel,
        eda_tool=inspection.eda_tool,
        sources=tuple(item.path for item in inspection.inputs),
        constraints=(),
        parameters=inspection.parameters,
        flow_options=inspection.flow_options,
    )
    plan = plan_coarse_simulation_campaign(
        handle=target.handle,
        inspection=inspection,
        preview=preview,
        selected_tests=names,
        required_suite=target.declared_tests,
        revision="abc123",
        invocation_id=1,
        execution_id="8" * 32,
        trace=False,
        kind="coverage_aggregate",
    )
    return plan, target


def _run_coverage_campaign(
    root: Path,
    native: NativeExecution,
    *,
    runtime_input: bool = False,
    pre_sim_commands: tuple[str, ...] = (),
    publication_checkpoint=None,
):
    plan, target = _coverage_campaign_plan(
        root, runtime_input=runtime_input, pre_sim_commands=pre_sim_commands
    )
    assert tuple(plan.manifest.document["workload"]["source_recipe"]["pre_sim_commands"]) == (
        pre_sim_commands
    )
    assert tuple(plan.manifest.document["work_items"][0]["selection"]["names"]) == (
        "reset",
        "wrap",
    )

    def execution_factory(_plan, _options, commands, _access):
        assert tuple(commands) == pre_sim_commands
        return native

    executor = CoverageAggregateExecutor(
        plans={target.handle.identity: target},
        execution_factory=execution_factory,
        publication_checkpoint=publication_checkpoint,
    )
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _admission()
    )
    outcome = SimulationCampaign(executor).run(request)
    return outcome, CampaignStore(invocation / "targets/sim_0/record")


_STAMP = "2026-09-22T00:00:00Z"


class _RawDocument:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def canonical_bytes(self) -> bytes:
        return self._raw


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _reference(path: str, raw: bytes, kind: str, owner: str) -> dict[str, object]:
    return {"path": path, "bytes": len(raw), "sha256": _digest(raw), "kind": kind, "owner": owner}


class _NoEdaExecutor:
    """Publish a fully authenticated design-failure chain without spawning EDA."""

    def __init__(self) -> None:
        self.calls = 0

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        raise AssertionError("unmanaged campaigns must not prepare child attempts")

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        self.calls += 1
        attempt, build_attempt, build_result, result = self.documents(request)
        request.store.publish_attempt(request.attempt_directory, attempt)
        build_root = request.attempt_directory / "private-build"
        build_root.mkdir()
        request.store.publish_build_attempt(build_root, build_attempt)
        request.store.publish_build_result(build_root, build_result)
        return result

    def documents(self, request: WorkExecutionRequest, *, validate: bool = True):
        common = _common(request)
        attempt = self._attempt(request, common, validate=validate)
        build_attempt = self._build_attempt(request, common, validate=validate)
        build_path = request.attempt_directory / "private-build/build-attempt.json"
        build_result = self._build_result(
            request, common, build_path, build_attempt, validate=validate
        )
        result_path = request.attempt_directory / "private-build/build-result.json"
        result = self._result(request, common, result_path, build_result, validate=validate)
        return attempt, build_attempt, build_result, result

    @staticmethod
    def _attempt(request: WorkExecutionRequest, common: dict[str, object], *, validate: bool):
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        run_cwd = cast(Mapping[str, object], workload["run_cwd"])
        document = {
            "$schema": "booley.simulation-attempt/v2",
            **common,
            "attempt_id": request.attempt_id,
            "attempt_ordinal": request.attempt_ordinal,
            "producer_invocation_id": request.producer_invocation_id,
            "build_variant_id": request.work_item["build_variant_id"],
            "run_directory": {
                "kind": run_cwd["kind"],
                "configured": run_cwd["configured"],
                "resolved": str(request.project_root),
                "collision_key": str(request.project_root),
                "owned": False,
            },
            "child_execution_id": request.child_execution_id,
            "child_entry_sha256": None,
            "pre_sim_build_access": workload["pre_sim_build_access"],
            "policy": {"timeout_seconds": None, "no_kill": False, "diagnostic": False},
            "started_at": _STAMP,
        }
        raw = canonical_json_bytes(document)
        return decode_simulation_attempt(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _build_attempt(
        request: WorkExecutionRequest, common: dict[str, object], *, validate: bool
    ):
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        eda = cast(Mapping[str, object], workload["eda"])
        document = {
            "$schema": "booley.bundle-build-attempt/v2",
            **{
                key: common[key]
                for key in ("simulation_record_id", "manifest_sha256", "workload_sha256")
            },
            "build_variant_id": request.work_item["build_variant_id"],
            "build_attempt_id": request.attempt_id,
            "build_attempt_ordinal": 1,
            "producer_invocation_id": request.producer_invocation_id,
            "sharing": "private_work_item",
            "owner": {
                "work_item_id": request.work_item["work_item_id"],
                "simulation_attempt_id": request.attempt_id,
            },
            "tool_provenance": {
                "eda_kind": eda["kind"],
                "eda_version": eda["version"],
                "adapter_contract_version": workload["adapter_contract_version"],
            },
            "started_at": _STAMP,
        }
        raw = canonical_json_bytes(document)
        return decode_bundle_build_attempt(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _build_result(request, common, attempt_path, attempt, *, validate):
        raw = attempt.canonical_bytes()
        document = {
            "$schema": "booley.bundle-build-result/v2",
            **{
                key: common[key]
                for key in ("simulation_record_id", "manifest_sha256", "workload_sha256")
            },
            "build_variant_id": request.work_item["build_variant_id"],
            "build_attempt": _reference(
                attempt_path.name, raw, "bundle_build_attempt", request.attempt_id
            )
            | {"build_attempt_id": request.attempt_id},
            "state": "design_failure",
            "phase": "compile",
            "finished_at": _STAMP,
            "elapsed_seconds": 0.0,
            "bundle": None,
            "observation": {
                "class": "design",
                "code": "synthetic",
                "message": "synthetic no-EDA build failure",
                "detail": {},
            },
            "evidence": [],
        }
        raw = canonical_json_bytes(document)
        return decode_bundle_build_result(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _result(request, common, result_path, build_result, *, validate):
        raw = build_result.canonical_bytes()
        selection = cast(Mapping[str, object], request.work_item["selection"])
        names = cast(tuple[str, ...], selection.get("names", ()))
        observation = {
            "test": names[0] if names else None,
            "execution": "blocked_by_build",
            "failure_class": "design",
            "functional": "not_observed",
            "assertions": "not_observed",
            "assertion_count": 0,
            "detail": {"reason": "synthetic no-EDA build failure"},
            "cycle_count": None,
        }
        observation["test_run"] = request.work_item["test_runs"][0]["name"]
        document = {
            "$schema": RESULT_SCHEMA,
            **common,
            "attempt_id": request.attempt_id,
            "attempt_ordinal": request.attempt_ordinal,
            "producer_invocation_id": request.producer_invocation_id,
            "state": "blocked_by_build",
            "build_result": _reference(
                "private-build/" + result_path.name, raw, "bundle_build_result", request.attempt_id
            )
            | {
                "build_attempt_id": request.attempt_id,
                "state": "design_failure",
                "sharing": "private_work_item",
            },
            "bundle_id": None,
            "finished_at": _STAMP,
            "elapsed_seconds": 0.0,
            "executable_snapshot": None,
            "runtime_inputs": [],
            "observations": [observation],
            "grade": "fail",
            "diagnostics": [],
            "evidence": [],
        }
        raw = canonical_json_bytes(document)
        return decode_simulation_result(raw) if validate else _RawDocument(raw)


def _common(request: WorkExecutionRequest) -> dict[str, object]:
    fingerprints = cast(Mapping[str, object], request.manifest.document["fingerprints"])
    return {
        "simulation_record_id": request.manifest.document["simulation_record_id"],
        "manifest_sha256": manifest_digest(request.manifest),
        "workload_sha256": fingerprints["workload_sha256"],
        "work_item_id": request.work_item["work_item_id"],
    }


def _retained_invocation(
    tmp_path: Path,
    *,
    reports: Path | None = None,
    booley_dir: Path | None = None,
) -> tuple[Path, Path, CampaignStore]:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".booley").mkdir()
    source_store = _completed(source).store
    project_data = tmp_path / "project-data"
    reports = reports or project_data / ".runtime" / "flow-reports"
    invocation = reports / "sim" / "1"
    target = invocation / "targets" / "sim"
    target.mkdir(parents=True)
    shutil.move(str(source_store.root), target / "record")
    # Reports under a symlinked Booley Directory entry need the store to follow it (#2191).
    store = CampaignStore(target / "record", **({"booley_dir": booley_dir} if booley_dir else {}))
    store.regenerate_summary()
    manifest = store.load_manifest().document
    manifest_target = manifest["target"]
    (target / "simulation.json").write_bytes(
        canonical_json_bytes(
            {
                "flow": "sim",
                "mode": "simulate",
                "complete": True,
                "target": "sim",
                "target_identity": (f"{manifest_target['vlnv']}#{manifest_target['name']}"),
                "passed": True,
                "inconclusive": False,
                "tests": [],
                "campaign_manifest": str(store.manifest_path),
                "campaign_summary": str(store.summary_path),
            }
        )
    )
    (invocation / "progress.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "complete": True,
                "phase": "complete",
                "targets": ["sim"],
                "completed_targets": ["sim"],
                "pending_targets": [],
            }
        ),
        encoding="utf-8",
    )
    return reports, project_data, store


def _own_attempt_run_directory(
    tmp_path: Path, store: CampaignStore, work_item_id: str, lane_id: str | None
) -> tuple[Path, dict[str, object]]:
    """Give one work item's attempt an owned run directory and the lane it ran on."""
    from booley.flows.sim.record.run_directory import expand_run_directory

    attempt_path = next(
        (store.work_item_directory(work_item_id) / "attempts").glob("*/attempt.json")
    )
    document = json.loads(attempt_path.read_bytes())
    project_root = tmp_path / "checkout"
    project_root.mkdir(exist_ok=True)
    run = expand_run_directory(
        "work/{record}/{test}/{attempt}",
        project_root=project_root,
        campaign_id=document["simulation_record_id"],
        target_key="sim",
        work_item_key=store.work_item_directory(work_item_id).name,
        attempt_key=attempt_path.parent.name,
    )
    document["run_directory"] = {
        "kind": "templated",
        "configured": "work/{record}/{test}/{attempt}",
        "resolved": str(run.path),
        "collision_key": run.collision_key,
        "owned": True,
    }
    document["child_execution_id"] = lane_id
    document["child_entry_sha256"] = None
    attempt_path.chmod(0o600)
    attempt_path.write_bytes(canonical_json_bytes(document))
    run.path.mkdir(parents=True)
    (run.path / ".booley-simulation-attempt.json").write_bytes(
        canonical_json_bytes(
            {
                "simulation_record_id": document["simulation_record_id"],
                "work_item_id": work_item_id,
                "attempt_id": document["attempt_id"],
            }
        )
    )
    return run.path, document


def _validated_resume(store, plan, target, root: Path) -> ValidatedResumeManifest:
    node = ValidatedManifestNode(
        store.manifest_path, plan.manifest, manifest_digest(plan.manifest)
    )
    return ValidatedResumeManifest(
        node,
        (),
        (target.handle,),
        (ValidatedTargetBinding(node, root.resolve(), target.handle),),
    )


@dataclass(frozen=True)
class CoverageInvocation:
    """One completed coverage run in the embedded layout the Simulation Flow writes."""

    reports: Path
    target: Path
    storage: Path

    @property
    def public(self) -> Path:
        """The Target's ``coverage.json`` reference."""
        return self.target / "coverage.json"

    @property
    def campaign_path(self) -> Path:
        """The Coverage Campaign the reference selects, in its nested directory."""
        return self.storage / "coverage.json"

    @property
    def simulation(self) -> Path:
        """The Simulation projection beside the reference."""
        return self.target / "simulation.json"


def coverage_invocation(tmp_path, monkeypatch, *, prepare=None) -> CoverageInvocation:
    """Run a coverage Simulation Flow over ``tmp_path`` and return its invocation layout.

    ``prepare(tmp_path)`` writes the project; the default is the two-test counter project.
    """
    from booley.flows.sim.coverage_reference import resolve_coverage_campaign_reference
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    (prepare or project)(tmp_path)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n', encoding="utf-8")
    reports = tmp_path / "reports"
    flow = SimulateFlow(
        coverage_execution=lambda _handle, _options, _cmds, _access: NativeExecution()
    )
    request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    assert flow.execute(request).exit_code == 0
    target = reports / "sim/1/targets/sim_0"
    storage = resolve_coverage_campaign_reference(target / "coverage.json").campaign_path.parent
    return CoverageInvocation(reports, target, storage)
