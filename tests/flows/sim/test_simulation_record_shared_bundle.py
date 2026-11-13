"""Shared Simulator Bundle reuse, private-build fallback and snapshot authentication."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim.execution.contract import (
    SimulationOptions,
)
from booley.flows.sim.execution.engine import (
    PreparedOrdinaryGroup,
    SimulationBuildSlotError,
)
from booley.flows.sim.record import serial_execution
from booley.flows.sim.record.codec import (
    MANIFEST_SCHEMA,
    RESULT_SCHEMA,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    WorkExecutionRequest,
)
from booley.flows.sim.record.model import create_simulation_campaign_plan
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
from booley.flows.sim.record.store import CampaignStore
from booley.flows.sim.runtime_inputs import (
    RuntimeInputError,
    materialize_campaign_runtime_inputs,
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
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
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
    assert [item["cycle_count"] for item in outcome.observations] == [1234, 1234]
    store = CampaignStore(outcome.manifest_path.parent)
    assert store.load_manifest().document["$schema"] == MANIFEST_SCHEMA
    results = _completed_result_documents(store)
    assert all(result["$schema"] == RESULT_SCHEMA for result in results)
    assert [result["observations"][0]["test_run"] for result in results] == ["alpha", "beta"]


def test_failed_shared_build_blocks_each_named_work_item_with_its_own_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _named_manifest(("tail", "quick"))
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
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

    store = CampaignStore(invocation / "targets/sim/record")
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
    store = CampaignStore(invocation / "targets/sim/record")
    _, seed_counters, validated = _seed_failed_campaign(
        store, manifest, project, handle, tmp_path / "seed-build"
    )
    resumed_counters = {"compile": 0, "launch": 0}

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


@pytest.mark.parametrize(
    ("kind", "names", "expected"),
    [
        ("default", (), ["sim"]),
        ("named", ("tail", "quick"), ["tail", "quick"]),
    ],
)
def test_shared_build_failure_preserves_each_selection_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    names: tuple[str, ...],
    expected: list[str],
) -> None:
    manifest = _single_item_selection_manifest(kind, names)
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()  # a literal run_cwd must exist before any build (#881)
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    SimulationCampaign(_failed_shared_executor(tmp_path / "engine-build", counters)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )

    result = _completed_result_documents(CampaignStore(invocation / "targets/sim/record"))[0]
    assert [item["test"] for item in result["observations"]] == expected


def _seed_failed_campaign(store, manifest, project, handle, build_root):
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "launch": 0}
    request = _work_request(store, manifest, items[0], project, handle)
    result = _failed_shared_executor(build_root, counters).execute(request)
    store.publish_result(items[0]["work_item_id"], result)
    node = ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest))
    validated = ValidatedResumeManifest(
        node,
        (),
        (handle,),
        (ValidatedTargetBinding(node, project.resolve(), handle),),
    )
    return items, counters, validated


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


def _assert_shared_campaign(outcome, compile_count, launches, bindings, invocation) -> None:
    assert outcome.complete is True
    assert compile_count == 1
    assert [item[0] for item in launches] == ["alpha", "beta"]
    assert bindings == [("beta",)]
    assert launches[0][1] != launches[1][1]
    assert [item[2] for item in launches] == [b"pristine", b"pristine"]
    assert all(not item[1].exists() for item in launches)
    store = CampaignStore(invocation / "targets/sim/record")
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


def test_literal_runtime_input_removes_only_attempt_owned_materialization(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "input.bin").write_bytes(b"runtime input")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    run = tmp_path / "literal-run"
    run.mkdir()
    retained = run / "retained.txt"
    retained.write_text("project-owned", encoding="utf-8")
    declaration = {
        "declaration_id": "sha256:" + "1" * 64,
        "source_artifact_path": "input.bin",
        "destination": "generated/nested/input.bin",
    }

    with materialize_campaign_runtime_inputs(
        bundle_root=bundle,
        attempt_root=attempt,
        run_cwd=run,
        declarations=(declaration,),
        owned_run_directory=False,
    ) as bindings:
        assert (run / "generated/nested/input.bin").read_bytes() == b"runtime input"
        assert bindings[0].owned is True

    assert not (run / "generated").exists()
    assert retained.read_text(encoding="utf-8") == "project-owned"
    assert (attempt / "runtime-inputs/generated/nested/input.bin").read_bytes() == b"runtime input"


def test_literal_runtime_input_conflict_preserves_existing_file(tmp_path: Path) -> None:
    """A different file already at the destination is a typed conflict and stays untouched."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "input.bin").write_bytes(b"runtime input")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    run = tmp_path / "literal-run"
    run.mkdir()
    existing = run / "input.bin"
    existing.write_bytes(b"project-owned")
    declaration = {
        "declaration_id": "sha256:" + "1" * 64,
        "source_artifact_path": "input.bin",
        "destination": "input.bin",
    }

    with (
        pytest.raises(RuntimeInputError, match="conflicts with an existing path"),
        materialize_campaign_runtime_inputs(
            bundle_root=bundle,
            attempt_root=attempt,
            run_cwd=run,
            declarations=(declaration,),
            owned_run_directory=False,
        ),
    ):
        pytest.fail("conflicting runtime input must not be exposed")

    assert existing.read_bytes() == b"project-owned"


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


def _mutate_build_attempt(case: str, completed: _Completed) -> None:
    path = completed.build_dir / "build-attempt.json"
    document = _read(path)
    if case == "campaign":
        document["simulation_record_id"] = str(uuid.uuid4())
    elif case in {"manifest", "workload"}:
        document[f"{case}_sha256"] = _BAD_DIGEST
    elif case == "variant":
        document["build_variant_id"] = "variant:" + "e" * 64
    elif case == "invocation":
        document["producer_invocation_id"] += 1
    elif case == "ordinal":
        document["build_attempt_ordinal"] += 1
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
    # Each case is tied to the store check it reaches.
    scope = case in {"ordinal", "owner", "tool_provenance"}
    message = {"variant": "variant is not in manifest"}.get(
        case, "Build Attempt scope" if scope else "Build Attempt identity"
    )
    with pytest.raises(SimulationCampaignIntegrityError, match=message):
        completed.store.recover_shared_build(variant, 1)


@pytest.mark.parametrize(
    "case",
    ["campaign", "owner", "digest", "artifact_remove"],
)
def test_shared_recovery_rejects_build_result_reference_mutations(
    tmp_path: Path, case: str
) -> None:
    completed = _completed(tmp_path)
    path = completed.build_dir / "build-result.json"
    document = _read(path)
    if case == "campaign":
        document["simulation_record_id"] = str(uuid.uuid4())
    elif case == "owner":
        document["build_attempt"]["owner"] = str(uuid.uuid4())
    elif case == "digest":
        document["build_attempt"]["sha256"] = _BAD_DIGEST
    else:
        document["bundle"]["artifacts"] = []
    _write(path, document)
    variant = document["build_variant_id"]
    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.recover_shared_build(variant, 1)


def _mutate_bundle(case: str, completed: _Completed) -> None:
    bundle_path = completed.build_dir / "evidence/bundle.json"
    bundle = _read(bundle_path)
    if case == "campaign":
        bundle["simulation_record_id"] = str(uuid.uuid4())
    elif case == "attempt":
        bundle["build_attempt_id"] = str(uuid.uuid4())
    elif case == "owner":
        bundle["owner"] = {
            "work_item_id": completed.item_id,
            "simulation_attempt_id": str(uuid.uuid4()),
        }
    elif case == "tool_provenance":
        bundle["tool_provenance"]["eda_version"] = "transplanted"
    else:
        bundle["artifacts"] = []
    _write(bundle_path, bundle)
    result_path = completed.build_dir / "build-result.json"
    result = _read(result_path)
    raw = bundle_path.read_bytes()
    result["bundle"]["manifest_bytes"], result["bundle"]["manifest_sha256"] = _identity(raw)
    _write(result_path, result)


@pytest.mark.parametrize(
    "case",
    ["campaign", "attempt", "owner", "tool_provenance", "artifact_remove"],
)
def test_shared_recovery_rejects_bundle_transplants(tmp_path: Path, case: str) -> None:
    completed = _completed(tmp_path)
    variant = _read(completed.build_dir / "build-attempt.json")["build_variant_id"]
    _mutate_bundle(case, completed)
    message = "Bundle identity" if case == "campaign" else "Bundle binding"
    with pytest.raises(SimulationCampaignIntegrityError, match=message):
        completed.store.recover_shared_build(variant, 1)


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
        "snapshot": document["executable_snapshot"]["pre_launch_sha256"],
    }


def test_recovered_bundle_preserves_verdict_log_trace_timeout_and_launch_contract(
    tmp_path: Path,
) -> None:
    manifest = _compatibility_manifest()
    store = CampaignStore(tmp_path / "record")
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


@pytest.mark.parametrize(
    "declared",
    ["../image/in.hex", "C:in.hex", ""],
)
def test_private_image_capture_refuses_an_unsafe_runtime_input_path(
    tmp_path: Path, declared: str
) -> None:
    """A resumed prerequisite manifest is not re-planned; capture must not copy outside."""
    source_root = tmp_path / "image"
    source_root.mkdir()
    (source_root / "sim.bin").write_bytes(b"x")
    (source_root / "in.hex").write_bytes(b"y")
    destination = tmp_path / "private"
    with pytest.raises(SimulationCampaignIntegrityError, match="invalid runtime input path"):
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
        SimulationBuildSlotError,
        match="recovered bundle evidence is not successful",
    ):
        group.bind_authenticated_bundle(evidence)

    assert group._build_process is None
    assert group._build is None


def test_shared_build_is_recovered_by_a_fresh_executor_and_scoped_per_invocation(
    tmp_path: Path,
) -> None:
    manifest = _manifest_for(("alpha", "beta"))
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "record")
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    first = _executor(build_root, counters).execute(
        _request(store, manifest, items[0], project, invocation=1)
    )
    second = _executor(build_root, counters).execute(
        _request(store, manifest, items[1], project, invocation=1)
    )
    third = _executor(build_root, counters).execute(
        _request(store, manifest, items[0], project, invocation=2)
    )

    assert counters == {
        "compile": 2,
        "durable_reuse": 1,
        "launch": 3,
    }
    assert first.document["build_result"]["sha256"] == second.document["build_result"]["sha256"]
    assert third.document["build_result"]["sha256"] != first.document["build_result"]["sha256"]
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 2


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
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    hooks: list[tuple[tuple[str, ...], bool, Path | None]] = []

    def record_hook(
        _handle: object,
        _root: Path,
        names: tuple[str, ...],
        _options: object,
        run_cwd: Path | None,
        expose_build_root: bool,
        *,
        commands: tuple[str, ...],
        test_runs: tuple[object, ...],
    ) -> None:
        assert commands == ()
        assert [run.name for run in test_runs] == list(names)
        from booley.flows.test_runs import test_run_document

        assert [test_run_document(run) for run in test_runs] == _selection_test_runs(names)
        hooks.append((names, expose_build_root, run_cwd))

    monkeypatch.setattr(serial_execution, "_run_hook", record_hook)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(_executor(build_root, counters, disclosures=disclosures)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )
    _assert_private_legacy_results(outcome, counters, hooks, invocation)
    if mapped:
        from booley.flows.sim.record.coordinator import ResumeCampaignRunRequest
        from tests.flows.sim.simulation_record_test_support import _validated_campaign

        store = CampaignStore(invocation / "targets/sim/record")
        validated = _validated_campaign(store, manifest, project, _handle(project))
        resumed = SimulationCampaign(_executor(build_root, counters, disclosures=disclosures)).run(
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
        assert resumed.complete
        assert counters["compile"] == 2
        assert store.load_manifest() == manifest


def _assert_private_legacy_results(outcome, counters, hooks, invocation) -> None:
    assert outcome.complete is True
    assert counters["compile"] == 2
    assert counters["durable_reuse"] == 0
    assert hooks == [(("alpha",), True, None), (("beta",), True, None)]
    store = CampaignStore(invocation / "targets/sim/record")
    private_results = tuple(
        store.root.glob("work-items/*/attempts/*/private-build/build-result.json")
    )
    assert len(private_results) == 2
    assert not tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))
    assert {
        json.loads((store.work_item_directory(item) / "result.json").read_text())["build_result"][
            "sharing"
        ]
        for item in store.scan().complete
    } == {"private_work_item"}


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_snapshot_mutation_fails_closed_for_shared_and_private_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    manifest = _manifest_for(("alpha",), access=access)
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    with pytest.raises(SimulationCampaignIntegrityError, match="snapshot changed"):
        SimulationCampaign(_executor(build_root, counters, mutate_snapshot=True)).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
            )
        )

    store = CampaignStore(invocation / "targets/sim/record")
    assert counters["launch"] == 1
    assert store.scan().pending == ()
    assert len(store.scan().interrupted) == 1
    assert not tuple(store.root.glob("work-items/*/result.json"))


def _run_failed_shared_build_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, work_item_attempt: bool
) -> dict[str, int]:
    """Run two items sharing a failing build, its compiler OOM-killed inside one attempt."""
    from booley.runtime.endpoint_execution import job_attempt, note_job_oom_kill

    manifest = _named_manifest(("tail", "quick"))
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir(parents=True)
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    # Publish the results as the Campaign would for an item that took no claim of its own.
    monkeypatch.setattr(
        "booley.flows.sim.record.coordinator.work_item_attempt_requeues", lambda **_: False
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    # An OOM-killed compiler is an infrastructure failure, which recovery never settles.
    executor = _failed_shared_executor(
        tmp_path / "engine-build", counters, note_job_oom_kill, infrastructure=True
    )

    with job_attempt(stop_on_oom=True, work_item=work_item_attempt):
        SimulationCampaign(executor).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
            )
        )
    return counters


def test_requeued_attempt_rebuilds_the_shared_build_its_kill_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The build was an OOM-killed work item attempt's own: a failure the item runs again
    # for must not be cached, so each attempt compiles the variant itself.
    item = _run_failed_shared_build_campaign(
        tmp_path / "item", monkeypatch, work_item_attempt=True
    )
    # A build killed under the Flow's own attempt is cached as before: it compiles once.
    flow = _run_failed_shared_build_campaign(
        tmp_path / "flow", monkeypatch, work_item_attempt=False
    )

    assert item["compile"] == 2
    assert flow["compile"] == 1
