"""Shared Simulator Bundle and attempt-isolation contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    WorkExecutionRequest,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
from booley.flows.sim.campaign.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    ValidatedTargetBinding,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from tests.flows.sim.test_campaign_manifest_codec import _manifest, _sha


def _admission() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


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


def _work_item(ordinal: int, name: str, target: object, variant_id: object) -> dict:
    identity = {
        "ordinal": ordinal,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": target,
        "selection": {"kind": "named", "names": [name]},
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


def test_shareable_variant_compiles_once_and_isolates_attempt_runtime_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _two_item_manifest()
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"pristine")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    compile_count = [0]
    launches: list[tuple[str, Path, bytes]] = []
    bindings: list[tuple[str, ...]] = []

    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )

    executor = _shared_executor(build_root, run_log, handle, launches, compile_count, bindings)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )
    _assert_shared_campaign(outcome, compile_count[0], launches, bindings, invocation)


def test_failed_shared_build_blocks_each_named_work_item_with_its_own_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _named_manifest(("tail", "quick"))
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    outcome = SimulationCampaign(_failed_shared_executor(tmp_path / "engine-build", counters)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )

    store = CampaignStore(invocation / "targets/sim/campaign")
    results = _completed_result_documents(store)
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert counters == {"compile": 1, "launch": 0}
    assert [result["state"] for result in results] == ["blocked_by_build"] * 2
    assert [result["observations"][0]["test"] for result in results] == ["tail", "quick"]
    assert all(
        result["observations"][0]["detail"]["reason"] == "compiler reason" for result in results
    )
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 1
    assert len({result["build_result"]["sha256"] for result in results}) == 1


def test_recovered_failed_shared_build_blocks_each_pending_item_with_its_own_selection(
    tmp_path: Path,
) -> None:
    manifest = _named_manifest(("tail", "quick", "smoke"))
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    handle = _handle(project)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    store = CampaignStore(invocation / "targets/sim/campaign")
    store.publish_manifest(manifest)  # type: ignore[arg-type]
    items = manifest.document["work_items"]  # type: ignore[attr-defined]
    seed_counters = {"compile": 0, "launch": 0}
    seed_executor = _failed_shared_executor(tmp_path / "seed-build", seed_counters)
    first_request = _work_request(store, manifest, items[0], project, handle)
    first = seed_executor.execute(first_request)
    store.verify_result_evidence(first_request.attempt_directory, first)
    store.publish_result(items[0]["work_item_id"], first)
    resumed_counters = {"compile": 0, "launch": 0}
    node = ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest))
    validated = ValidatedResumeManifest(
        node,
        (),
        (handle,),
        (ValidatedTargetBinding(node, project.resolve(), handle),),
    )

    outcome = SimulationCampaign(
        _failed_shared_executor(tmp_path / "resumed-build", resumed_counters)
    ).run(
        ResumeCampaignRunRequest(
            validated,
            plan,
            project,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            _admission(),
        )
    )

    results = _completed_result_documents(store)
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert seed_counters == {"compile": 1, "launch": 0}
    assert resumed_counters == {"compile": 0, "launch": 0}
    assert [result["observations"][0]["test"] for result in results] == [
        "tail",
        "quick",
        "smoke",
    ]
    assert [result["observations"][0]["detail"]["reason"] for result in results] == [
        "compiler reason",
        "compiler tail",
        "compiler tail",
    ]
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 1
    assert len({result["build_result"]["sha256"] for result in results}) == 1


def test_unfiltered_cocotb_shared_build_failure_has_one_unnamed_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _unfiltered_cocotb_manifest()
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    outcome = SimulationCampaign(_failed_shared_executor(tmp_path / "engine-build", counters)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )

    store = CampaignStore(invocation / "targets/sim/campaign")
    result = _completed_result_documents(store)[0]
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert result["grade"] == "fail"
    assert result["observations"] == [
        {
            "test": None,
            "execution": "blocked_by_build",
            "failure_class": "design",
            "functional": "not_observed",
            "assertions": "not_observed",
            "assertion_count": 0,
            "detail": {"reason": "compiler reason"},
            "cycle_count": None,
        }
    ]


def _handle(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )


def _failed_shared_executor(
    build_root: Path, counters: dict[str, int]
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
            return SimpleNamespace(passed=False)

        def finish_build_failure(self):
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

        def launch_snapshot(self, *_args, **_kwargs):
            counters["launch"] += 1
            raise AssertionError("failed shared build must not launch")

    class FailedExecution:
        @contextmanager
        def ordinary_group(self, _handle, _names):
            yield FailedGroup()

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FailedExecution(),  # type: ignore[arg-type,return-value]
    )


def _work_request(store, manifest, item, project, handle) -> WorkExecutionRequest:
    attempt_id = str(uuid.uuid4())
    ordinal, directory = store.allocate_attempt_directory(item["work_item_id"], attempt_id)
    return WorkExecutionRequest(
        store,
        manifest,
        item,
        attempt_id,
        ordinal,
        directory,
        1,
        CampaignPolicy(),
        _admission(),
        project,
        handle,
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

        def reuse_compilation_from(self, source) -> None:
            raise AssertionError(f"shared cache retained work-item group {source.names}")

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
        def ordinary_group(self, _handle, names):
            yield FakeGroup(names)

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
    )


def _shared_outcome(name, handle, run_log):
    test = SimulationTestOutcome(name=name, verdict="pass", passed=True, run_log_path=str(run_log))
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


def _assert_shared_campaign(outcome, compile_count, launches, bindings, invocation) -> None:
    assert outcome.complete is True
    assert compile_count == 1
    assert [item[0] for item in launches] == ["alpha", "beta"]
    assert bindings == [("beta",)]
    assert launches[0][1] != launches[1][1]
    assert [item[2] for item in launches] == [b"pristine", b"pristine"]
    assert all(not item[1].exists() for item in launches)
    store = CampaignStore(invocation / "targets/sim/campaign")
    build_results = tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))
    assert len(build_results) == 1
    result_documents = [
        json.loads((store.work_item_directory(item) / "result.json").read_text())
        for item in store.scan().complete
    ]
    assert {item["build_result"]["sha256"] for item in result_documents} == {
        "sha256:" + hashlib.sha256(build_results[0].read_bytes()).hexdigest()
    }
    assert {item["build_result"]["sharing"] for item in result_documents} == {"shared_variant"}
