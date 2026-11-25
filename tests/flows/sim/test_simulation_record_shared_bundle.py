"""Shared Simulator Bundle reuse, private-build fallback and in-place verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.execution.engine import PreparedOrdinaryGroup, SimulationBuildSlotError
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import serial_execution
from booley.flows.sim.record.codec import (
    MANIFEST_SCHEMA,
    RESULT_SCHEMA,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    ValidatedTargetBinding,
)
from booley.flows.sim.record.serial_execution import (
    OrdinaryHdlSerialExecutor,
    capture_private_image,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.runtime_inputs import (
    RuntimeInputError,
    materialize_simulation_record_runtime_inputs,
)
from tests.flows.sim.simulation_record_boundary import (
    PASS_PROCESS,
    Launch,
    run_simulation_record_sim,
)
from tests.flows.sim.simulation_record_test_support import (
    _BAD_DIGEST,
    _admission,
    _build_execution,
    _Completed,
    _completed,
    _completed_result_documents,
    _create_build_root,
    _executor,
    _failed_shared_executor,
    _handle,
    _legacy_disclosures,
    _manifest_for,
    _named_manifest,
    _read,
    _request,
    _shared_executor,
    _single_item_selection_manifest,
    _TestExecution,
    _two_item_manifest,
    _write,
)
from tests.flows.sim.test_simulation_record_manifest_codec import _selection_test_runs, _sha


def _project_with_build(tmp_path: Path) -> tuple[Path, Path]:
    """Return an empty project root and an engine build root holding one image."""
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    return project, build_root


def test_shareable_variant_compiles_once_and_isolates_attempt_runtime_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, build_root = _project_with_build(tmp_path)
    (build_root / "input.bin").write_bytes(b"pristine")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    compile_count = [0]
    launches: list[tuple[str, Path, bytes]] = []
    bindings: list[tuple[str, ...]] = []
    executor = _shared_executor(
        build_root, run_log, _handle(project), launches, compile_count, bindings
    )
    outcome, store = _run_new_simulation_record(
        tmp_path, monkeypatch, _two_item_manifest(), executor, project=project
    )
    invocation = tmp_path / "reports" / "000001"
    _assert_shared_simulation_record(outcome, compile_count[0], launches, bindings, invocation)
    assert [item["cycle_count"] for item in outcome.observations] == [1234, 1234]
    assert store.load_manifest().document["$schema"] == MANIFEST_SCHEMA
    results = _completed_result_documents(store)
    assert all(result["$schema"] == RESULT_SCHEMA for result in results)
    assert [result["observations"][0]["test_run"] for result in results] == ["alpha", "beta"]


def _run_new_simulation_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Any,
    executor: Any,
    *,
    literal_run: bool = False,
    project: Path | None = None,
) -> tuple[Any, SimulationRecordStore]:
    """Run ``manifest`` as a new Simulation Record under ``executor``; return outcome and store."""
    plan = create_simulation_record_plan(manifest)
    project = project or tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    if literal_run:
        (project / "run").mkdir()  # a literal run_cwd must exist before any build (#881)
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    outcome = SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan, project, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
        )
    )
    return outcome, SimulationRecordStore(invocation / "targets/sim/record")


def _assert_one_shared_failure(store: SimulationRecordStore, results: list[Any]) -> None:
    """Every blocked item reports the one failed shared build, published once."""
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 1
    assert len({result["build_result"]["sha256"] for result in results}) == 1


def test_failed_shared_build_blocks_each_named_work_item_with_its_own_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counters = {"compile": 0, "launch": 0}
    outcome, store = _run_new_simulation_record(
        tmp_path,
        monkeypatch,
        _named_manifest(("tail", "quick")),
        _failed_shared_executor(tmp_path / "engine-build", counters),
    )

    results = _completed_result_documents(store)
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert counters == {"compile": 1, "launch": 0}
    assert [result["state"] for result in results] == ["blocked_by_build"] * 2
    assert [result["observations"][0]["test"] for result in results] == ["tail", "quick"]
    assert all(
        result["observations"][0]["detail"]["reason"] == "compiler reason" for result in results
    )
    _assert_one_shared_failure(store, results)


def test_recovered_failed_shared_build_blocks_each_pending_item_with_its_own_selection(
    tmp_path: Path,
) -> None:
    manifest = _named_manifest(("tail", "quick", "smoke"))
    plan = create_simulation_record_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    handle = _handle(project)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    store = SimulationRecordStore(invocation / "targets/sim/record")
    _, seed_counters, validated = _seed_failed_simulation_record(
        store, manifest, project, handle, tmp_path / "seed-build"
    )
    resumed_counters = {"compile": 0, "launch": 0}

    outcome = SimulationRecord(
        _failed_shared_executor(tmp_path / "resumed-build", resumed_counters)
    ).run(
        ResumeSimulationRecordRunRequest(
            validated,
            plan,
            project,
            invocation.parent,
            SimulationRecordPolicy(),
            invocation,
            _admission(),
        )
    )

    results = _completed_result_documents(store)
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert seed_counters == {"compile": 1, "launch": 0}
    assert resumed_counters == {"compile": 0, "launch": 0}
    assert [result["observations"][0]["test"] for result in results] == ["tail", "quick", "smoke"]
    assert [result["observations"][0]["detail"]["reason"] for result in results] == [
        "compiler reason",
        "compiler tail",
        "compiler tail",
    ]
    _assert_one_shared_failure(store, results)


@pytest.mark.parametrize(
    ("kind", "names", "expected"),
    [("default", (), ["sim"]), ("named", ("tail", "quick"), ["tail", "quick"])],
)
def test_shared_build_failure_preserves_each_selection_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    names: tuple[str, ...],
    expected: list[str],
) -> None:
    counters = {"compile": 0, "launch": 0}
    _outcome, store = _run_new_simulation_record(
        tmp_path,
        monkeypatch,
        _single_item_selection_manifest(kind, names),
        _failed_shared_executor(tmp_path / "engine-build", counters),
        literal_run=True,
    )

    result = _completed_result_documents(store)[0]
    assert [item["test"] for item in result["observations"]] == expected


def _seed_failed_simulation_record(store, manifest, project, handle, build_root):
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "launch": 0}
    request = _request(store, manifest, items[0], project, invocation=1)
    result = _failed_shared_executor(build_root, counters).execute(request)
    store.publish_result(items[0]["work_item_id"], result)
    node = ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest))
    validated = ValidatedResumeManifest(
        node, (), (handle,), (ValidatedTargetBinding(node, project.resolve(), handle),)
    )
    return items, counters, validated


def _assert_shared_simulation_record(
    outcome, compile_count, launches, bindings, invocation
) -> None:
    assert outcome.complete is True
    assert compile_count == 1
    assert [item[0] for item in launches] == ["alpha", "beta"]
    assert bindings == []
    assert launches[0][1] != launches[1][1]
    assert [item[2] for item in launches] == [b"pristine", b"pristine"]
    assert all(not item[1].exists() for item in launches)
    store = SimulationRecordStore(invocation / "targets/sim/record")
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


@pytest.mark.parametrize("conflict", [False, True])
def test_literal_runtime_input_materialization_is_attempt_owned_or_a_typed_conflict(
    tmp_path: Path, conflict: bool
) -> None:
    """A different file at the destination is a typed conflict and stays untouched."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "input.bin").write_bytes(b"runtime input")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    run = tmp_path / "literal-run"
    run.mkdir()
    existing = run / ("input.bin" if conflict else "retained.txt")
    existing.write_bytes(b"project-owned")
    declaration = {
        "declaration_id": "sha256:" + "1" * 64,
        "source_artifact_path": "input.bin",
        "destination": "input.bin" if conflict else "generated/nested/input.bin",
    }
    materialized = materialize_simulation_record_runtime_inputs(
        bundle_root=bundle,
        attempt_root=attempt,
        run_cwd=run,
        declarations=(declaration,),
        owned_run_directory=False,
        booley_dir=tmp_path / ".booley",
    )

    if conflict:
        with (
            pytest.raises(RuntimeInputError, match="conflicts with an existing path"),
            materialized,
        ):
            pytest.fail("conflicting runtime input must not be exposed")
    else:
        with materialized as bindings:
            assert (run / "generated/nested/input.bin").read_bytes() == b"runtime input"
            assert bindings[0].owned is True
        assert not (run / "generated").exists()
        assert not (tmp_path / ".booley/.runtime/staged-runtime-inputs").exists() or not list(
            (tmp_path / ".booley/.runtime/staged-runtime-inputs").glob("*.json")
        )
        assert (attempt / "runtime-inputs/generated/nested/input.bin").read_bytes() == (
            b"runtime input"
        )
    assert existing.read_bytes() == b"project-owned"


_STAGED_DECLARATION = {
    "declaration_id": "sha256:" + "2" * 64,
    "source_artifact_path": "input.bin",
    "destination": "generated/input.bin",
}


def _staging(tmp_path: Path, attempt: str):
    """Stage ``generated/input.bin`` into the literal run of *tmp_path* for one attempt."""
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "input.bin").write_bytes(b"runtime input")
    (tmp_path / attempt).mkdir()
    (tmp_path / "run").mkdir(exist_ok=True)
    return materialize_simulation_record_runtime_inputs(
        bundle_root=bundle,
        attempt_root=tmp_path / attempt,
        run_cwd=tmp_path / "run",
        declarations=(_STAGED_DECLARATION,),
        owned_run_directory=False,
        booley_dir=tmp_path / ".booley",
    )


def _die_staging(tmp_path: Path, *, inside_copy: bool = False) -> None:
    """Stage in a forked worker that dies, like a SIGKILL, before it can clean up."""
    from tests.flows.sim.test_simulation_record_crash_matrix import _KILLED, _run_killed

    def worker() -> None:
        if inside_copy:
            real = shutil.copyfile

            def dying(source, destination, **kwargs):
                if Path(destination).is_relative_to(tmp_path / "run"):
                    os._exit(_KILLED)
                return real(source, destination, **kwargs)

            shutil.copyfile = dying  # type: ignore[assignment]
        with _staging(tmp_path, "attempt-killed"):
            os._exit(_KILLED)

    _run_killed(worker, tmp_path / "trace.txt")


def _records(tmp_path: Path) -> list[Path]:
    return list((tmp_path / ".booley/.runtime/staged-runtime-inputs").glob("*.json"))


def _fails_with(tmp_path: Path, attempt: str, match: str) -> str:
    """Stage once more and return the message of the error that fails the run."""
    with pytest.raises(RuntimeInputError, match=match) as caught, _staging(tmp_path, attempt):
        pytest.fail("the run must fail before it exposes its inputs")
    return str(caught.value)


def _modified_leftover_is_named(tmp_path: Path, destination: Path) -> None:
    with destination.open("r+b") as stream:
        stream.write(b"RUNTIME")
    message = _fails_with(tmp_path, "attempt-2", "cancelled Booley run left it there")
    assert "generated/input.bin" in message
    assert "safe to delete" in message
    assert destination.read_bytes() == b"RUNTIME input"
    assert len(_records(tmp_path)) == 1


def _symlinked_parent_is_refused(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "input.bin").write_bytes(b"runtime input")
    (tmp_path / "run/generated").rename(tmp_path / "moved")
    (tmp_path / "run/generated").symlink_to(user, target_is_directory=True)
    _fails_with(tmp_path, "attempt-2", "contains a symlink")
    assert (user / "input.bin").read_bytes() == b"runtime input"


def _replayed_record_deletes_nothing(tmp_path: Path, destination: Path) -> None:
    """The record of an already-swept run, replayed against a new file, deletes nothing."""
    record = _records(tmp_path)[0]
    stale = record.read_bytes()
    swept = destination.stat().st_ino
    with _staging(tmp_path, "attempt-2"):
        pass
    assert not destination.parent.exists()
    destination.parent.mkdir()
    holders = tmp_path / "holders"
    holders.mkdir()
    # A filesystem may hand the swept inode number to the next file; hold it so the
    # replacement is a different file, as the record's identity check assumes.
    for index in range(1000):
        destination.write_bytes(b"runtime input")
        if destination.stat().st_ino != swept:
            break
        destination.rename(holders / str(index))
    assert destination.stat().st_ino != swept
    record.write_bytes(stale)
    record.with_suffix(".lock").touch()
    _fails_with(tmp_path, "attempt-3", "cancelled Booley run left it there")
    assert destination.read_bytes() == b"runtime input"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="the kill worker forks")
@pytest.mark.parametrize(
    "case",
    ["unchanged", "modified", "user-file", "pending", "parent-symlink", "stale-record-replay"],
)
def test_killed_staging_leftover_is_removed_or_named(tmp_path: Path, case: str) -> None:
    """A killed run's copy goes; a user's file, or one the user changed, stays."""
    destination = tmp_path / "run/generated/input.bin"
    if case == "user-file":
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"runtime input")
    _die_staging(tmp_path, inside_copy=case == "pending")
    assert destination.exists() is (case != "pending")
    assert len(_records(tmp_path)) == (case != "user-file")

    if case == "modified":
        return _modified_leftover_is_named(tmp_path, destination)
    if case == "parent-symlink":
        return _symlinked_parent_is_refused(tmp_path)
    if case == "stale-record-replay":
        return _replayed_record_deletes_nothing(tmp_path, destination)
    if case == "pending":
        destination.write_bytes(b"runtime input")
    with _staging(tmp_path, "attempt-2") as bindings:
        assert destination.read_bytes() == b"runtime input"
        assert bindings[0].owned is (case == "unchanged")
    assert destination.exists() is (case in {"user-file", "pending"})
    assert destination.parent.exists() is (case in {"user-file", "pending"})
    assert _records(tmp_path) == []
    return None


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


def _identity(raw: bytes) -> tuple[int, str]:
    return len(raw), "sha256:" + hashlib.sha256(raw).hexdigest()


def _refresh_reference(reference: dict[str, Any], path: Path) -> None:
    raw = path.read_bytes()
    reference["bytes"], reference["sha256"] = _identity(raw)


def _transplant(document: dict[str, Any], case: str, completed: _Completed) -> None:
    """Give a Build Attempt or bundle document another owner or another tool version."""
    if case == "owner":
        document["owner"] = {
            "work_item_id": completed.item_id,
            "simulation_attempt_id": str(uuid.uuid4()),
        }
    else:
        document["tool_provenance"]["eda_version"] = "transplanted"


def _mutate_build_attempt(case: str, completed: _Completed) -> None:
    path = completed.build_dir / "build-attempt.json"
    document = _read(path)
    if case == "record":
        document["simulation_record_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        # Not a production name: the document key is built as "<case>_sha256".
        document[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "variant":
        document["build_variant_id"] = "variant:" + "e" * 64
    elif case == "invocation":
        document["producer_invocation_id"] += 1
    elif case == "ordinal":
        document["build_attempt_ordinal"] += 1
    elif case in {"owner", "tool_provenance"}:
        _transplant(document, case, completed)
    _write(path, document)
    result_path = completed.build_dir / "build-result.json"
    result = _read(result_path)
    _refresh_reference(result["build_attempt"], path)
    _write(result_path, result)


def _mutate_build_result(case: str, completed: _Completed) -> None:
    path = completed.build_dir / "build-result.json"
    document = _read(path)
    if case == "record":
        document["simulation_record_id"] = str(uuid.uuid4())
    elif case == "owner":
        document["build_attempt"]["owner"] = str(uuid.uuid4())
    elif case == "digest":
        document["build_attempt"]["sha256"] = _BAD_DIGEST
    else:
        document["bundle"]["artifacts"] = []
    _write(path, document)


def _mutate_bundle(case: str, completed: _Completed) -> None:
    bundle_path = completed.build_dir / "evidence/bundle.json"
    bundle = _read(bundle_path)
    if case == "record":
        bundle["simulation_record_id"] = str(uuid.uuid4())
    elif case == "attempt":
        bundle["build_attempt_id"] = str(uuid.uuid4())
    elif case in {"owner", "tool_provenance"}:
        _transplant(bundle, case, completed)
    else:
        bundle["artifacts"] = []
    _write(bundle_path, bundle)
    result_path = completed.build_dir / "build-result.json"
    result = _read(result_path)
    raw = bundle_path.read_bytes()
    result["bundle"]["manifest_bytes"], result["bundle"]["manifest_sha256"] = _identity(raw)
    _write(result_path, result)


_ATTEMPT_IDENTITY = "Build Attempt identity"
_ATTEMPT_SCOPE = "Build Attempt scope"
_RECOVERY_REJECTIONS = [
    ("attempt", "record", _ATTEMPT_IDENTITY),
    ("attempt", "manifest", _ATTEMPT_IDENTITY),
    ("attempt", "workload", _ATTEMPT_IDENTITY),
    ("attempt", "variant", "variant is not in manifest"),
    ("attempt", "ordinal", _ATTEMPT_SCOPE),
    ("attempt", "owner", _ATTEMPT_SCOPE),
    ("attempt", "tool_provenance", _ATTEMPT_SCOPE),
    ("result", "record", None),
    ("result", "owner", None),
    ("result", "digest", None),
    ("result", "artifact_remove", None),
    ("bundle", "record", "Bundle identity"),
    ("bundle", "attempt", "Bundle binding"),
    ("bundle", "owner", "Bundle binding"),
    ("bundle", "tool_provenance", "Bundle binding"),
    ("bundle", "artifact_remove", "Bundle binding"),
]


@pytest.mark.parametrize(("target", "case", "message"), _RECOVERY_REJECTIONS)
def test_shared_recovery_rejects_transplanted_build_evidence(
    tmp_path: Path, target: str, case: str, message: str | None
) -> None:
    """Each mutation of the Build Attempt, Build Result or bundle reaches its own store check."""
    completed = _completed(tmp_path)
    mutate = {
        "attempt": _mutate_build_attempt,
        "result": _mutate_build_result,
        "bundle": _mutate_bundle,
    }[target]
    mutate(case, completed)
    variant = _read(completed.build_dir / "build-attempt.json")["build_variant_id"]
    with pytest.raises(SimulationRecordIntegrityError, match=message):
        completed.store.recover_shared_build(variant, 1)


def test_shared_recovery_ignores_a_build_attempt_of_another_invocation(tmp_path: Path) -> None:
    completed = _completed(tmp_path)
    _mutate_build_attempt("invocation", completed)
    variant = _read(completed.build_dir / "build-attempt.json")["build_variant_id"]
    assert completed.store.recover_shared_build(variant, 1) is None


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
    observation = document["observations"][0]
    assert observation["test_run"] == observation["test"]
    observation["test"] = observation["test_run"] = "selected"
    return {
        "state": document["state"],
        "grade": document["grade"],
        "observations": document["observations"],
        "evidence": [
            (item["kind"], item["bytes"], item["sha256"]) for item in document["evidence"]
        ],
        "build_result": document["build_result"]["sha256"],
        "bundle": document["bundle_verification"]["pre_launch_sha256"],
    }


def test_recovered_bundle_preserves_verdict_log_trace_timeout_and_launch_contract(
    tmp_path: Path,
) -> None:
    manifest = _compatibility_manifest()
    store = SimulationRecordStore(tmp_path / "record")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = _create_build_root(tmp_path / "engine-build")
    items = manifest.document["work_items"]
    seen: list[SimulationOptions] = []
    policy = SimulationRecordPolicy(timeout_seconds=7)
    first, second = (
        _recording_executor(build_root, seen).execute(
            _request(store, manifest, item, project, invocation=1, policy=policy)
        )
        for item in items[:2]
    )

    assert _normalized_result(first) == _normalized_result(second)
    assert seen == [
        SimulationOptions(trace=True, timeout_ms=7000),
        SimulationOptions(trace=True, timeout_ms=7000),
    ]


@pytest.mark.parametrize("declared", ["../image/in.hex", "C:in.hex", ""])
def test_private_image_capture_refuses_an_unsafe_runtime_input_path(
    tmp_path: Path, declared: str
) -> None:
    """A resumed prerequisite manifest is not re-planned; capture must not copy outside."""
    source_root = tmp_path / "image"
    source_root.mkdir()
    (source_root / "sim.bin").write_bytes(b"x")
    (source_root / "in.hex").write_bytes(b"y")
    destination = tmp_path / "private"
    with pytest.raises(SimulationRecordIntegrityError, match="invalid runtime input path"):
        capture_private_image(
            (source_root / "sim.bin",),
            source_root,
            destination,
            ({"source_artifact_path": declared},),  # type: ignore[arg-type]
        )
    assert not (tmp_path / "in.hex").exists()
    assert sorted(path.name for path in destination.iterdir()) == []


def test_authenticated_bundle_rejects_timed_out_compiler_process() -> None:
    evidence = _build_execution()
    evidence["process"]["timed_out"] = True  # type: ignore[index]
    group = PreparedOrdinaryGroup.__new__(PreparedOrdinaryGroup)
    group._lease_active = True
    group._build_process = None
    group._build = None

    with pytest.raises(
        SimulationBuildSlotError, match="recovered bundle evidence is not successful"
    ):
        group.bind_authenticated_bundle(evidence)

    assert group._build_process is None
    assert group._build is None


def test_shared_build_is_recovered_by_a_fresh_executor_and_scoped_per_invocation(
    tmp_path: Path,
) -> None:
    manifest = _manifest_for(("alpha", "beta"))
    project, build_root = _project_with_build(tmp_path)
    store = SimulationRecordStore(tmp_path / "record")
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    launched: list[Path] = []

    first = _executor(build_root, counters, launched_bundles=launched).execute(
        _request(store, manifest, items[0], project, invocation=1)
    )
    second = _executor(build_root, counters, launched_bundles=launched).execute(
        _request(store, manifest, items[1], project, invocation=1)
    )
    third = _executor(build_root, counters, launched_bundles=launched).execute(
        _request(store, manifest, items[0], project, invocation=2)
    )

    assert counters == {"compile": 2, "durable_reuse": 1, "launch": 3}
    assert first.document["build_result"]["sha256"] == second.document["build_result"]["sha256"]
    assert third.document["build_result"]["sha256"] != first.document["build_result"]["sha256"]
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 2
    # The recovered bundle is the one launched: second runs the first's, not a rebuild.
    assert launched[0] == launched[1] != launched[2]
    for result, bundle in zip((first, second, third), launched, strict=True):
        assert (bundle / "build-result.json").is_file()
        assert result.document["build_result"]["path"].endswith("build-result.json")
        assert result.document["bundle_verification"]["verified_after_exit"] is True


@pytest.mark.parametrize("mapped", [False, True])
def test_legacy_mode_builds_and_discloses_each_work_item_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped: bool
) -> None:
    disclosures = _legacy_disclosures()
    if mapped:
        for disclosure in disclosures.values():
            disclosure["generated_files"] = [
                {
                    "path": "staged.hex",
                    "source_path": "data/source.hex",
                    "bytes": 1,
                    "sha256": "sha256:" + "a" * 64,
                    "kind": "generated_input",
                }
            ]
    base_manifest = _manifest_for(("alpha", "beta"), access="legacy-per-test")
    manifest_document = json.loads(canonical_json_bytes(base_manifest.document))
    manifest_document.pop("fingerprints")
    manifest_document["planning_disclosures"] = list(disclosures.values())
    manifest = finalize_manifest(manifest_document)
    plan = create_simulation_record_plan(manifest)
    project, build_root = _project_with_build(tmp_path)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    hooks: list[tuple[tuple[str, ...], bool, Path | None]] = []
    launched: list[Path] = []

    def record_hook(
        _handle: object,
        *,
        test_names: tuple[str, ...],
        run_cwd: str | None,
        expose_build_root: bool,
        commands: tuple[str, ...],
        test_runs: tuple[object, ...],
        **_unused: object,
    ) -> None:
        assert commands == ()
        assert [run.name for run in test_runs] == list(test_names)
        from booley.flows.test_runs import test_run_document

        assert [test_run_document(run) for run in test_runs] == _selection_test_runs(test_names)
        hooks.append((test_names, expose_build_root, run_cwd))

    monkeypatch.setattr(serial_execution, "run_pre_sim_commands", record_hook)
    monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _handle: {})
    outcome, store = _run_new_simulation_record(
        tmp_path,
        monkeypatch,
        manifest,
        _executor(build_root, counters, disclosures=disclosures, launched_bundles=launched),
        project=project,
    )
    invocation = tmp_path / "reports" / "000001"
    _assert_private_legacy_results(outcome, store, counters, hooks, launched)
    if mapped:
        from booley.flows.sim.record.coordinator import ResumeSimulationRecordRunRequest
        from tests.flows.sim.simulation_record_test_support import _validated_simulation_record

        validated = _validated_simulation_record(store, manifest, project, _handle(project))
        resumed = SimulationRecord(_executor(build_root, counters, disclosures=disclosures)).run(
            ResumeSimulationRecordRunRequest(
                validated,
                plan,
                project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                _admission(),
            )
        )
        assert resumed.complete
        assert counters["compile"] == 2
        assert store.load_manifest() == manifest


def _assert_private_legacy_results(outcome, store, counters, hooks, launched) -> None:
    assert outcome.complete is True
    assert counters["compile"] == 2
    assert counters["durable_reuse"] == 0
    assert hooks == [(("alpha",), True, None), (("beta",), True, None)]
    private_results = tuple(
        store.root.glob("work-items/*/attempts/*/private-build/build-result.json")
    )
    assert len(private_results) == 2
    # Each attempt launches, in place, the private build it made.
    assert sorted(launched) == sorted(path.parent for path in private_results)
    assert not tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))
    assert {
        json.loads((store.work_item_directory(item) / "result.json").read_text())["build_result"][
            "sharing"
        ]
        for item in store.scan().complete
    } == {"private_work_item"}


_BUNDLE_KINDS = {
    "shared": ("immutable", "build-variants/*/attempts/*"),
    "private": ("legacy-per-test", "work-items/*/attempts/*/private-build"),
}


def _bundle_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str,
    names: tuple[str, ...] = ("alpha",),
    observe_hooks: bool = False,
    **executor_options: Any,
) -> tuple[Path, dict[str, int], list[Path], Any]:
    """Prepare one record run; return its invocation, counters, launches and a run callable."""
    manifest = _manifest_for(names, access=_BUNDLE_KINDS[kind][0])
    plan = create_simulation_record_plan(manifest)
    project, build_root = _project_with_build(tmp_path)
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    launched: list[Path] = []
    hooks: list[dict[str, object]] = []
    if observe_hooks:
        monkeypatch.setattr(
            serial_execution, "run_pre_sim_commands", lambda *_args, **kwargs: hooks.append(kwargs)
        )
        monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _h: {})
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    executor = _executor(build_root, counters, launched_bundles=launched, **executor_options)

    def run() -> object:
        return SimulationRecord(executor).run(
            NewSimulationRecordRunRequest(
                plan,
                project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                _admission(),
            )
        )

    run.hooks = hooks  # type: ignore[attr-defined]
    return invocation, counters, launched, run


def _bundle_directories(invocation: Path, kind: str) -> list[Path]:
    store = SimulationRecordStore(invocation / "targets/sim/record")
    return sorted(store.root.glob(_BUNDLE_KINDS[kind][1]))


def test_shared_bundle_stores_the_simulator_once_and_every_attempt_launches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation, counters, launched, run = _bundle_run(
        tmp_path, monkeypatch, kind="shared", names=("alpha", "beta", "gamma"), observe_hooks=True
    )

    assert run().complete is True  # type: ignore[attr-defined]

    store = SimulationRecordStore(invocation / "targets/sim/record")
    (bundle,) = _bundle_directories(invocation, "shared")
    assert counters == {"compile": 1, "durable_reuse": 0, "launch": 3}
    assert launched == [bundle, bundle, bundle]
    copies = [
        path for path in store.root.rglob("*") if path.is_file() and path.read_bytes() == b"image"
    ]
    assert copies == [bundle / "simv"]
    assert not tuple(store.root.glob("work-items/*/attempts/*/snapshot"))
    # Immutable Pre-Sim Commands never receive the bundle: no build root is exposed.
    assert len(run.hooks) == 3  # type: ignore[attr-defined]
    for kwargs in run.hooks:  # type: ignore[attr-defined]
        assert kwargs["expose_build_root"] is False
        assert str(bundle) not in repr(kwargs)


@pytest.mark.parametrize("mutation", ["changed_artifact", "undeclared_entry", "before_launch"])
@pytest.mark.parametrize("kind", ["shared", "private"])
def test_bundle_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mutation: str
) -> None:
    """A changed bundle is refused during an attempt, and one changed before launch never launches."""
    from tests.flows.sim.simulation_record_test_support import mutate_bundle_directory

    before_launch = mutation == "before_launch"
    options: dict[str, Any] = (
        {
            "checkpoint": lambda boundary: (
                mutate_bundle_directory(
                    _bundle_directories(invocation, kind)[0], "undeclared_entry"
                )
                if boundary == "after:runtime_inputs"
                else None
            )
        }
        if before_launch
        else {"mutate_bundle": mutation}
    )
    invocation, counters, _launched, run = _bundle_run(tmp_path, monkeypatch, kind=kind, **options)
    expected = "bundle artifact changed" if mutation == "changed_artifact" else "undeclared bundle"

    with pytest.raises(SimulationRecordIntegrityError, match=expected):
        run()

    store = SimulationRecordStore(invocation / "targets/sim/record")
    assert counters["launch"] == (0 if before_launch else 1)
    if not before_launch:
        assert store.scan().pending == ()
        assert len(store.scan().interrupted) == 1
    assert not tuple(store.root.glob("work-items/*/result.json"))


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX permission bits")
@pytest.mark.parametrize("kind", ["shared", "private"])
def test_ready_bundle_files_are_read_only_and_the_simulator_stays_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    invocation, _counters, _launched, run = _bundle_run(tmp_path, monkeypatch, kind=kind)
    assert run().complete is True  # type: ignore[attr-defined]

    (bundle,) = _bundle_directories(invocation, kind)
    manifest = json.loads((bundle / "evidence/bundle.json").read_text(encoding="utf-8"))
    artifacts = manifest["artifacts"]
    assert {artifact["path"] for artifact in artifacts} == {"simv"}
    for artifact in artifacts:
        mode = (bundle / artifact["path"]).stat().st_mode
        assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        assert (mode & stat.S_IXUSR != 0) is (artifact["kind"] == "simulator_executable")
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (bundle / artifact["path"]).open("r+b")


def _ready_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run one shared record and return its bundle document, build result and directory."""
    from booley.flows.sim.record.codec import decode_bundle_build_result, decode_simulator_bundle

    invocation, _counters, _launched, run = _bundle_run(tmp_path, monkeypatch, kind="shared")
    assert run().complete is True  # type: ignore[attr-defined]
    (directory,) = _bundle_directories(invocation, "shared")
    bundle = decode_simulator_bundle((directory / "evidence/bundle.json").read_bytes())
    result = decode_bundle_build_result((directory / "build-result.json").read_bytes())
    return bundle, result, directory


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("manifest", "bundle manifest changed"),
        ("evidence", "bundle evidence changed"),
        ("missing", "bundle artifact is unavailable"),
        ("stray_directory", "undeclared bundle entry"),
        ("stray_in_evidence", "undeclared bundle entry"),
        ("stray_link", "undeclared bundle entry"),
        ("hard_link", "another hard link"),
        ("unreadable_digest", "unreadable"),
        ("unreadable_scandir", "unreadable"),
    ],
)
def test_in_place_verification_refuses_every_change_to_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, message: str
) -> None:
    from booley.flows.sim.record import bundle as bundle_module
    from tests.conftest import require_symlinks, symlink_or_skip

    bundle, result, directory = _ready_bundle(tmp_path, monkeypatch)
    verify = bundle_module.verify_bundle_in_place
    # An untouched bundle verifies to its recorded inventory.
    assert verify(bundle, result, directory) == bundle.document["inventory_sha256"]

    def unreadable(*_args: object, **_kwargs: object) -> Any:
        raise PermissionError("denied")

    if change == "stray_link":
        require_symlinks(tmp_path)
        symlink_or_skip(directory / "stray", directory / "simv")
    elif change == "stray_directory":
        (directory / "stray").mkdir()
    elif change == "stray_in_evidence":
        (directory / "evidence" / "stray.json").write_bytes(b"{}")
    elif change == "missing":
        (directory / "simv").unlink()
    elif change == "hard_link":
        os.link(directory / "simv", tmp_path / "alias")
    elif change == "unreadable_digest":
        monkeypatch.setattr(bundle_module, "stream_digest", unreadable)
    elif change == "unreadable_scandir":
        monkeypatch.setattr(os, "scandir", unreadable)
    else:
        name = "bundle.json" if change == "manifest" else "build-execution.json"
        (directory / "evidence" / name).write_bytes(b"changed")

    with pytest.raises(SimulationRecordIntegrityError, match=message):
        verify(bundle, result, directory)


def _run_failed_shared_build_simulation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, work_item_attempt: bool
) -> dict[str, int]:
    """Run two items sharing a failing build, its compiler OOM-killed inside one attempt."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    from booley.runtime.endpoint_execution import job_attempt, note_job_oom_kill

    # Publish the results as the Simulation Record would for an item that took no claim of its own.
    monkeypatch.setattr(
        "booley.flows.sim.record.coordinator.work_item_attempt_requeues", lambda **_: False
    )
    counters = {"compile": 0, "launch": 0}
    # An OOM-killed compiler is an infrastructure failure, which recovery never settles.
    executor = _failed_shared_executor(
        tmp_path / "engine-build", counters, note_job_oom_kill, infrastructure=True
    )

    with job_attempt(stop_on_oom=True, work_item=work_item_attempt):
        _run_new_simulation_record(
            tmp_path, monkeypatch, _named_manifest(("tail", "quick")), executor
        )
    return counters


def test_requeued_attempt_rebuilds_the_shared_build_its_kill_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The build was an OOM-killed work item attempt's own: a failure the item runs again
    # for must not be cached, so each attempt compiles the variant itself.
    item = _run_failed_shared_build_simulation_record(
        tmp_path / "item", monkeypatch, work_item_attempt=True
    )
    # A build killed under the Flow's own attempt is cached as before: it compiles once.
    flow = _run_failed_shared_build_simulation_record(
        tmp_path / "flow", monkeypatch, work_item_attempt=False
    )

    assert item["compile"] == 2
    assert flow["compile"] == 1


def test_forty_test_runs_configure_their_shared_build_once(tmp_path: Path, monkeypatch) -> None:
    """Every lane after the first launches from the retained preparation (#4843)."""
    names = [f"t{index:02d}" for index in range(40)]

    run = run_simulation_record_sim(tmp_path, monkeypatch, {"sim": names}, PASS_PROCESS)

    assert run.result.exit_code == 0, run.result.outcome.report_text
    assert run.launched == [("sim", (name,)) for name in names]
    # 40 disclosure preparations from planning (one per work item), then one build.
    assert run.configures == 41


def test_resume_configures_its_recovered_shared_build_once(tmp_path: Path, monkeypatch) -> None:
    """A build recovered from disk is configured by the first lane that needs it only."""
    for module in ("flow", "flow_resume", "record.resume"):
        monkeypatch.setattr(f"booley.flows.sim.{module}.git_full_sha", lambda *_a: "a" * 40)

    def interrupt_at_t1(launch: Launch) -> SubprocessResult:
        if launch.tests == ("t1",):
            raise KeyboardInterrupt("interrupted at t1")
        return PASS_PROCESS

    targets = {"sim": ["t0", "t1", "t2", "t3"]}
    flow = SimulateFlow()
    with pytest.raises(KeyboardInterrupt, match="interrupted at t1"):
        run_simulation_record_sim(tmp_path, monkeypatch, targets, interrupt_at_t1, flow=flow)
    (manifest,) = (tmp_path / "reports").rglob("manifest.json")

    resumed = run_simulation_record_sim(
        tmp_path, monkeypatch, targets, PASS_PROCESS, flow=flow, resume_from=manifest
    )

    assert resumed.result.exit_code == 0, resumed.result.outcome.report_text
    assert resumed.launched == [("sim", (name,)) for name in ("t1", "t2", "t3")]
    # Resume re-plans one disclosure per group (4), then configures the recovered build once.
    assert resumed.configures == 5


def test_a_failed_confirmation_unwinds_the_copy_it_follows(tmp_path: Path, monkeypatch) -> None:
    from booley.flows.sim.staged_input_ledger import StagedInputLedger

    real_created = StagedInputLedger.created

    def failing(self: StagedInputLedger, relative: Path) -> None:
        if relative.name == "input.bin":
            raise OSError("disk full")
        real_created(self, relative)

    monkeypatch.setattr(StagedInputLedger, "created", failing)
    with pytest.raises(OSError, match="disk full"), _staging(tmp_path, "attempt"):
        pytest.fail("a failed confirmation must not expose the input")

    assert not (tmp_path / "run/generated").exists()
