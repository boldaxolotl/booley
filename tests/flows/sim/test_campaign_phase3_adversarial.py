"""Adversarial recovery and isolation checks for Phase 3 campaigns."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import CampaignPolicy
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.run_directory import (
    RunDirectory,
    claimed_run_directory,
    cleanup_interrupted_run_directory,
    expand_run_directory,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from tests.flows.sim.test_campaign_manifest_codec import _sha
from tests.flows.sim.test_campaign_phase3_integrity import (
    _manifest_for,
    _request,
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
            project_root=build_root.parent.resolve(), authored_paths=(), operational_paths=()
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
    def ordinary_group(self, _handle: object, names: tuple[str, ...]):
        yield _TestGroup(self._build_root, names[0])


def _executor(build_root: Path) -> OrdinaryHdlSerialExecutor:
    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _TestExecution(build_root),  # type: ignore[arg-type,return-value]
    )


def _recording_executor(
    build_root: Path, seen: list[SimulationOptions]
) -> OrdinaryHdlSerialExecutor:
    def factory(options: SimulationOptions) -> _TestExecution:
        seen.append(options)
        return _TestExecution(build_root)

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=factory,  # type: ignore[arg-type]
    )


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.chmod(0o700)
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(document))


def _identity(raw: bytes) -> tuple[int, str]:
    return len(raw), "sha256:" + hashlib.sha256(raw).hexdigest()


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
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    item = manifest.document["work_items"][0]
    request = _request(store, manifest, item, project, invocation=1)
    result = _executor(build_root).execute(request)
    store.publish_result(item["work_item_id"], result)
    assert store.scan().complete == (item["work_item_id"],)
    build_dir = next(store.root.glob("build-variants/*/attempts/*"))
    return _Completed(store, item["work_item_id"], request.attempt_directory, build_dir)


def _refresh_reference(reference: dict[str, Any], path: Path) -> None:
    raw = path.read_bytes()
    reference["bytes"], reference["sha256"] = _identity(raw)


def _mutate_build_attempt(case: str, completed: _Completed) -> None:
    path = completed.build_dir / "build-attempt.json"
    document = _read(path)
    if case == "campaign":
        document["campaign_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        document[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "variant":
        document["build_variant_id"] = "variant:" + "e" * 64
    elif case == "invocation":
        document["producer_invocation_id"] += 1
    elif case == "ordinal":
        document["build_attempt_ordinal"] += 1
    elif case == "sharing":
        document["sharing"] = "private_work_item"
        document["owner"] = {
            "work_item_id": completed.item_id,
            "simulation_attempt_id": str(uuid.uuid4()),
        }
    elif case == "owner":
        document["owner"] = {
            "work_item_id": completed.item_id,
            "simulation_attempt_id": str(uuid.uuid4()),
        }
    elif case == "tool_provenance":
        document["tool_provenance"]["eda_version"] = "transplanted"
    _write(path, document)
    result_path = completed.build_dir / "build-result.json"
    result = _read(result_path)
    _refresh_reference(result["build_attempt"], path)
    _write(result_path, result)


@pytest.mark.parametrize(
    "case",
    [
        "campaign",
        "manifest",
        "workload",
        "variant",
        "invocation",
        "ordinal",
        "sharing",
        "owner",
        "tool_provenance",
    ],
)
def test_shared_recovery_rejects_build_attempt_transplants(tmp_path: Path, case: str) -> None:
    completed = _completed(tmp_path)
    _mutate_build_attempt(case, completed)
    variant = _read(completed.build_dir / "build-attempt.json")["build_variant_id"]
    if case == "invocation":
        assert completed.store.recover_shared_build(variant, 1) is None
        return
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.recover_shared_build(variant, 1)


@pytest.mark.parametrize(
    "case",
    [
        "campaign",
        "manifest",
        "workload",
        "variant",
        "owner",
        "digest",
        "sharing",
        "state_bundle",
        "artifact_remove",
        "artifact_add",
        "artifact_reorder",
    ],
)
def test_shared_recovery_rejects_build_result_reference_mutations(
    tmp_path: Path, case: str
) -> None:
    completed = _completed(tmp_path)
    path = completed.build_dir / "build-result.json"
    document = _read(path)
    if case == "campaign":
        document["campaign_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        document[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "variant":
        document["build_variant_id"] = "variant:" + "e" * 64
    elif case == "owner":
        document["build_attempt"]["owner"] = str(uuid.uuid4())
    elif case == "digest":
        document["build_attempt"]["sha256"] = _BAD_DIGEST
    elif case == "sharing":
        document["bundle"]["sharing"] = "private_work_item"
    elif case == "state_bundle":
        document["state"] = "design_failure"
    elif case == "artifact_remove":
        document["bundle"]["artifacts"] = []
    elif case == "artifact_add":
        document["bundle"]["artifacts"].append(dict(document["bundle"]["artifacts"][0]))
    else:
        document["bundle"]["artifacts"].reverse()
    if case == "state_bundle":
        document["bundle"] = None
    _write(path, document)
    variant = document["build_variant_id"]
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.recover_shared_build(variant, 1)


def _mutate_bundle(case: str, completed: _Completed) -> None:
    bundle_path = completed.build_dir / "evidence/bundle.json"
    bundle = _read(bundle_path)
    if case == "campaign":
        bundle["campaign_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        bundle[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "variant":
        bundle["build_variant_id"] = "variant:" + "e" * 64
    elif case == "attempt":
        bundle["build_attempt_id"] = str(uuid.uuid4())
    elif case == "owner":
        bundle["owner"] = {
            "work_item_id": completed.item_id,
            "simulation_attempt_id": str(uuid.uuid4()),
        }
    elif case == "tool_provenance":
        bundle["tool_provenance"]["eda_version"] = "transplanted"
    elif case == "inventory":
        bundle["inventory_sha256"] = _BAD_DIGEST
    elif case == "artifact_remove":
        bundle["artifacts"] = []
    elif case == "artifact_add":
        bundle["artifacts"].append(dict(bundle["artifacts"][0]))
    elif case == "artifact_reorder":
        bundle["artifacts"].reverse()
    _write(bundle_path, bundle)
    result_path = completed.build_dir / "build-result.json"
    result = _read(result_path)
    raw = bundle_path.read_bytes()
    result["bundle"]["manifest_bytes"], result["bundle"]["manifest_sha256"] = _identity(raw)
    _write(result_path, result)


@pytest.mark.parametrize(
    "case",
    [
        "campaign",
        "manifest",
        "workload",
        "variant",
        "attempt",
        "owner",
        "tool_provenance",
        "inventory",
        "artifact_remove",
        "artifact_add",
        "artifact_reorder",
    ],
)
def test_shared_recovery_rejects_bundle_transplants(tmp_path: Path, case: str) -> None:
    completed = _completed(tmp_path)
    variant = _read(completed.build_dir / "build-attempt.json")["build_variant_id"]
    _mutate_bundle(case, completed)
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.recover_shared_build(variant, 1)


def _rewrite_result_snapshot_reference(completed: _Completed) -> None:
    result_path = completed.store.work_item_directory(completed.item_id) / "result.json"
    result = _read(result_path)
    snapshot_path = completed.attempt_dir / result["executable_snapshot"]["manifest"]["path"]
    _refresh_reference(result["executable_snapshot"]["manifest"], snapshot_path)
    _write(result_path, result)


@pytest.mark.parametrize(
    "case",
    [
        "campaign",
        "manifest",
        "workload",
        "work_item",
        "attempt",
        "build_result",
        "bundle",
        "inventory",
        "artifact_remove",
        "artifact_add",
        "artifact_reorder",
    ],
)
def test_scan_rejects_complete_snapshot_identity_mutations(tmp_path: Path, case: str) -> None:
    completed = _completed(tmp_path)
    path = completed.attempt_dir / "snapshot/snapshot.json"
    snapshot = _read(path)
    if case == "campaign":
        snapshot["campaign_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        snapshot[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "work_item":
        snapshot["work_item_id"] = "item:9999:" + "e" * 16
    elif case == "attempt":
        snapshot["attempt_id"] = str(uuid.uuid4())
    elif case == "build_result":
        snapshot["build_result"]["owner"] = str(uuid.uuid4())
    elif case == "bundle":
        snapshot["bundle_id"] = str(uuid.uuid4())
    elif case == "inventory":
        snapshot["inventory_sha256"] = _BAD_DIGEST
    elif case == "artifact_remove":
        snapshot["artifacts"] = []
    elif case == "artifact_add":
        snapshot["artifacts"].append(dict(snapshot["artifacts"][0]))
    else:
        snapshot["artifacts"].reverse()
    _write(path, snapshot)
    _rewrite_result_snapshot_reference(completed)
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.scan()


@pytest.mark.parametrize(
    "case",
    [
        "campaign",
        "manifest",
        "workload",
        "work_item",
        "attempt",
        "ordinal",
        "invocation",
        "build_owner",
        "build_digest",
        "build_sharing",
        "bundle",
        "snapshot_pre",
        "snapshot_post",
    ],
)
def test_scan_rejects_complete_result_identity_mutations(tmp_path: Path, case: str) -> None:
    completed = _completed(tmp_path)
    path = completed.store.work_item_directory(completed.item_id) / "result.json"
    result = _read(path)
    if case == "campaign":
        result["campaign_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        result[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "work_item":
        result["work_item_id"] = "item:9999:" + "e" * 16
    elif case == "attempt":
        result["attempt_id"] = str(uuid.uuid4())
    elif case == "ordinal":
        result["attempt_ordinal"] += 1
    elif case == "invocation":
        result["producer_invocation_id"] += 1
    elif case == "build_owner":
        result["build_result"]["owner"] = str(uuid.uuid4())
    elif case == "build_digest":
        result["build_result"]["sha256"] = _BAD_DIGEST
    elif case == "build_sharing":
        result["build_result"]["sharing"] = "private_work_item"
    elif case == "bundle":
        result["bundle_id"] = str(uuid.uuid4())
    elif case == "snapshot_pre":
        result["executable_snapshot"]["pre_launch_sha256"] = _BAD_DIGEST
    else:
        result["executable_snapshot"]["post_exit_sha256"] = _BAD_DIGEST
    _write(path, result)
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.scan()


def _compatibility_manifest():
    base = _manifest_for(("alpha", "beta"))
    document = json.loads(canonical_json_bytes(base.document))
    document.pop("fingerprints")
    document["workload"]["trace"] = True
    variant = document["build_variants"][0]
    recipe = {
        "kind": variant["kind"],
        "source_closure": variant["source_closure"],
        "source_recipe": document["workload"]["source_recipe"],
        "build_recipe": document["workload"]["build_recipe"],
        "eda": document["workload"]["eda"],
        "trace": True,
        "coverage": document["workload"]["coverage"],
    }
    variant["recipe_sha256"] = _sha(recipe)
    variant["build_variant_id"] = "variant:" + variant["recipe_sha256"].removeprefix("sha256:")
    for item in document["work_items"]:
        item["build_variant_id"] = variant["build_variant_id"]
        identity = {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
        item["fingerprint_sha256"] = _sha(identity)
        item["work_item_id"] = (
            f"item:{item['ordinal']:04d}:"
            + item["fingerprint_sha256"].removeprefix("sha256:")[:16]
        )
    return finalize_manifest(document)


def _normalized_result(result: object) -> dict[str, object]:
    document = json.loads(result.canonical_bytes())  # type: ignore[attr-defined]
    document["observations"][0]["test"] = "selected"
    return {
        "state": document["state"],
        "grade": document["grade"],
        "observations": document["observations"],
        "evidence": [
            (item["kind"], item["bytes"], item["sha256"]) for item in document["evidence"]
        ],
        "build_result": document["build_result"]["sha256"],
        "snapshot": document["executable_snapshot"]["pre_launch_sha256"],
    }


def test_recovered_bundle_preserves_verdict_log_trace_timeout_and_launch_contract(
    tmp_path: Path,
) -> None:
    manifest = _compatibility_manifest()
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = _create_build_root(tmp_path / "engine-build")
    items = manifest.document["work_items"]
    seen: list[SimulationOptions] = []
    first_request = replace(
        _request(store, manifest, items[0], project, invocation=1),
        policy=CampaignPolicy(timeout_seconds=7),
    )
    first = _recording_executor(build_root, seen).execute(first_request)
    second_request = replace(
        _request(store, manifest, items[1], project, invocation=1),
        policy=CampaignPolicy(timeout_seconds=7),
    )
    second = _recording_executor(build_root, seen).execute(second_request)

    assert _normalized_result(first) == _normalized_result(second)
    assert seen == [
        SimulationOptions(trace=True, timeout_ms=7000),
        SimulationOptions(trace=True, timeout_ms=7000),
    ]


def _run_identity() -> dict[str, str]:
    return {
        "campaign_id": str(uuid.uuid4()),
        "work_item_id": "item:0000:" + "a" * 16,
        "attempt_id": str(uuid.uuid4()),
    }


def test_stale_owned_run_tree_is_cleaned_only_for_exact_owner(tmp_path: Path) -> None:
    identity = _run_identity()
    run = RunDirectory(tmp_path / "run", str(tmp_path / "run"), True, tmp_path / "run.lock")
    with claimed_run_directory(run, identity=identity) as directory:
        (directory / "nested").mkdir()
        (directory / "nested/output.log").write_text("stale", encoding="utf-8")
        marker = (directory / ".booley-simulation-attempt.json").read_bytes()
    run.path.mkdir()
    (run.path / ".booley-simulation-attempt.json").write_bytes(marker)
    (run.path / "stale.log").write_text("stale", encoding="utf-8")
    assert cleanup_interrupted_run_directory(run, identity=identity) is True
    assert not run.path.exists()


@pytest.mark.parametrize("marker", ["missing", "mismatched"])
def test_stale_run_cleanup_preserves_unmarked_or_mismatched_tree(
    tmp_path: Path, marker: str
) -> None:
    identity = _run_identity()
    run = RunDirectory(tmp_path / "run", str(tmp_path / "run"), True, tmp_path / "run.lock")
    run.path.mkdir()
    payload = run.path / "preserve.log"
    payload.write_text("mine", encoding="utf-8")
    if marker == "mismatched":
        wrong = _run_identity()
        (run.path / ".booley-simulation-attempt.json").write_bytes(canonical_json_bytes(wrong))
    with pytest.raises(SimulationCampaignIntegrityError):
        cleanup_interrupted_run_directory(run, identity=identity)
    assert payload.read_text(encoding="utf-8") == "mine"


def test_canonical_collision_keys_are_stable_across_threads(tmp_path: Path) -> None:
    literal = tmp_path / "literal"
    literal.mkdir()
    first = expand_run_directory(
        str(literal),
        project_root=tmp_path,
        campaign_id=str(uuid.uuid4()),
        target_key="sim",
        work_item_key="item",
        attempt_key="attempt",
    )
    alias = expand_run_directory(
        str(literal / ".." / "literal"),
        project_root=tmp_path,
        campaign_id=str(uuid.uuid4()),
        target_key="sim",
        work_item_key="item",
        attempt_key="attempt",
    )
    barrier = threading.Barrier(2)
    keys: list[str] = []

    def claim(run: RunDirectory) -> None:
        barrier.wait()
        with claimed_run_directory(run, identity=_run_identity()):
            keys.append(run.collision_key)

    threads = [threading.Thread(target=claim, args=(run,)) for run in (first, alias)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert first.collision_key == alias.collision_key
    assert keys == [first.collision_key, first.collision_key]
