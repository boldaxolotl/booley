from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.record.codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_simulation_attempt,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_simulation_attempt,
    encode_simulation_result,
    encode_simulator_bundle,
    validate_relative_path,
)
from booley.flows.sim.record.model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
    SimulationResult,
    SimulatorBundle,
    grade_observations,
    simulation_status_from_observations,
)


def _attempt() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-attempt/v2",
        "simulation_record_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "workload_sha256": "sha256:" + "2" * 64,
        "work_item_id": "item:0000:0123456789abcdef",
        "attempt_id": "550e8400-e29b-41d4-a716-446655440000",
        "attempt_ordinal": 1,
        "producer_invocation_id": 1,
        "build_variant_id": "variant:" + "3" * 64,
        "run_directory": {
            "kind": "literal",
            "configured": "run",
            "resolved": "run",
            "collision_key": "run",
            "owned": True,
        },
        "child_execution_id": None,
        "child_entry_sha256": None,
        "pre_sim_build_access": "immutable",
        "policy": {"timeout_seconds": None, "no_kill": False, "diagnostic": False},
        "started_at": "2026-09-21T10:00:00Z",
    }


def test_directly_constructed_documents_deep_freeze() -> None:
    source = _attempt()
    value = SimulationAttempt(source)
    source["run_directory"]["owned"] = False  # type: ignore[index]
    assert value.document["run_directory"]["owned"] is True  # type: ignore[index]
    with pytest.raises(TypeError):
        value.document["run_directory"]["owned"] = False  # type: ignore[index]


@pytest.mark.parametrize(
    ("name", "decoder", "encoder"),
    [
        ("attempt.json", decode_simulation_attempt, encode_simulation_attempt),
        ("attempt-child.json", decode_simulation_attempt, encode_simulation_attempt),
        ("build-attempt-shared.json", decode_bundle_build_attempt, encode_bundle_build_attempt),
        ("build-attempt-private.json", decode_bundle_build_attempt, encode_bundle_build_attempt),
        ("bundle.json", decode_simulator_bundle, encode_simulator_bundle),
    ],
)
def test_document_encoders_round_trip_validated_values(name, decoder, encoder) -> None:
    raw = (Path(__file__).parents[1] / "fixtures/simulation_record" / name).read_bytes()
    value = decoder(raw)
    assert encoder(value) == raw
    assert value.canonical_bytes() == raw


@pytest.mark.parametrize(
    ("value_type", "encoder", "ceiling"),
    [
        pytest.param(SimulationAttempt, encode_simulation_attempt, RECORD_MAX_BYTES, id="attempt"),
        pytest.param(
            BundleBuildAttempt, encode_bundle_build_attempt, RECORD_MAX_BYTES, id="build-attempt"
        ),
        pytest.param(
            BundleBuildResult, encode_bundle_build_result, RECORD_MAX_BYTES, id="build-result"
        ),
        pytest.param(SimulationResult, encode_simulation_result, RECORD_MAX_BYTES, id="result"),
        pytest.param(SimulatorBundle, encode_simulator_bundle, MANIFEST_MAX_BYTES, id="bundle"),
    ],
)
def test_encoders_refuse_documents_over_their_size_ceiling(value_type, encoder, ceiling) -> None:
    """Publication encodes without decoding, so the encoder enforces the readers' ceiling."""
    overhead = len(canonical_json_bytes({"pad": ""}))
    at_ceiling = value_type({"pad": "x" * (ceiling - overhead)})
    assert len(encoder(at_ceiling)) == ceiling
    over_ceiling = value_type({"pad": "x" * (ceiling - overhead + 1)})
    with pytest.raises(SimulationRecordIntegrityError, match="size ceiling"):
        encoder(over_ceiling)


def test_codec_rejects_oversized_json() -> None:
    with pytest.raises(SimulationRecordIntegrityError, match="size ceiling"):
        decode_simulation_attempt(b" " * (MANIFEST_MAX_BYTES + 1))


@pytest.mark.parametrize(
    "mutation", [pytest.param(lambda document: document.pop("started_at"), id="missing-field")]
)
def test_attempt_field_mutation_is_rejected(mutation) -> None:
    mutated = _attempt()
    mutation(mutated)
    with pytest.raises(SimulationRecordIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(mutated))


@pytest.mark.parametrize("path", ["", "/root", "../escape", "a/../b", "a//b", "C:/run", "a\\b"])
def test_simulation_record_paths_reject_traversal_and_nonportable_forms(path: str) -> None:
    with pytest.raises(SimulationRecordIntegrityError):
        validate_relative_path(path)


@pytest.mark.parametrize("path", ["run", "work-items/item_0/result"])
def test_simulation_record_paths_accept_normalized_components(path: str) -> None:
    assert validate_relative_path(path) == path


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (("completed", None, "pass", "clean"), "pass"),
        (("completed", None, "pass", "dirty"), "fail"),
        (("completed", None, "inconclusive", "clean"), "inconclusive"),
        (("timeout", "design", "not_observed", "not_observed"), "fail"),
        (("setup_error", "infrastructure", "not_observed", "not_observed"), "error"),
    ],
)
def test_grade_table_is_exhaustive(
    observation: tuple[str, str | None, str, str], expected: str
) -> None:
    execution, failure, functional, assertions = observation
    assert (
        grade_observations(
            ExecutionObservation(execution),
            FailureClass(failure) if failure else None,
            FunctionalObservation(functional),
            AssertionObservation(assertions),
        ).value
        == expected
    )


@pytest.mark.parametrize(
    ("executions", "expected"),
    [
        (("completed", "timeout"), "timeout"),
        (("timeout", "crash"), "crash"),
        (("crash", "aborted"), "aborted"),
        (("timeout", "crash", "aborted"), "aborted"),
    ],
)
def test_simulation_status_uses_codec_execution_strength(executions, expected) -> None:
    observations = tuple(
        {"execution": execution, "functional": "not_observed"} for execution in executions
    )

    assert simulation_status_from_observations(observations) == expected


def test_store_rejects_manifest_and_result_republication(tmp_path):
    from booley.flows.sim.record.codec import decode_simulation_result
    from tests.flows.sim.simulation_record_test_support import _completed

    completed = _completed(tmp_path)
    store = completed.store
    result_path = store.work_item_directory(completed.item_id) / "result.json"
    before = (store.manifest_path.read_bytes(), result_path.read_bytes())
    with pytest.raises(FileExistsError):
        store.publish_manifest(store.load_manifest())
    with pytest.raises(FileExistsError):
        store.publish_result(completed.item_id, decode_simulation_result(before[1]))
    assert (store.manifest_path.read_bytes(), result_path.read_bytes()) == before
