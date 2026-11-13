from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booley.flows.sim.record import codec
from booley.flows.sim.record.codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulation_campaign_manifest,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_executable_snapshot,
    encode_simulation_attempt,
    encode_simulation_campaign_manifest,
    encode_simulation_result,
    encode_simulator_bundle,
    validate_relative_path,
)
from booley.flows.sim.record.model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
    SimulationResult,
    SimulatorBundle,
    create_simulation_campaign_plan,
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


def test_attempt_codec_is_canonical_and_immutable() -> None:
    value = decode_simulation_attempt(canonical_json_bytes(_attempt()))
    assert value.document["attempt_ordinal"] == 1
    assert canonical_json_bytes(value.document).endswith(b"\n")
    with pytest.raises(TypeError):
        value.document["attempt_ordinal"] = 2  # type: ignore[index]


def test_directly_constructed_documents_deep_freeze() -> None:
    source = _attempt()
    value = SimulationAttempt(source)
    source["run_directory"]["owned"] = False  # type: ignore[index]
    assert value.document["run_directory"]["owned"] is True  # type: ignore[index]
    with pytest.raises(TypeError):
        value.document["run_directory"]["owned"] = False  # type: ignore[index]


def test_encoding_and_planning_never_decode_their_own_output(monkeypatch) -> None:
    """Writers trust documents that were decoded once; they do not re-decode them."""
    from tests.flows.sim.test_simulation_record_manifest_codec import _manifest

    manifest = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    attempt = decode_simulation_attempt(canonical_json_bytes(_attempt()))

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("encoding decoded its own output")

    monkeypatch.setattr(codec, "_decode", refuse)
    assert encode_simulation_attempt(attempt) == canonical_json_bytes(attempt.document)
    assert encode_simulation_campaign_manifest(manifest) == canonical_json_bytes(manifest.document)
    assert create_simulation_campaign_plan(manifest).manifest is manifest


@pytest.mark.parametrize(
    ("name", "decoder", "encoder"),
    [
        ("attempt.json", decode_simulation_attempt, encode_simulation_attempt),
        ("attempt-child.json", decode_simulation_attempt, encode_simulation_attempt),
        ("build-attempt-shared.json", decode_bundle_build_attempt, encode_bundle_build_attempt),
        ("build-attempt-private.json", decode_bundle_build_attempt, encode_bundle_build_attempt),
        ("bundle.json", decode_simulator_bundle, encode_simulator_bundle),
        ("snapshot.json", decode_executable_snapshot, encode_executable_snapshot),
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
        pytest.param(
            ExecutableSnapshot, encode_executable_snapshot, MANIFEST_MAX_BYTES, id="snapshot"
        ),
    ],
)
def test_encoders_refuse_documents_over_their_size_ceiling(value_type, encoder, ceiling) -> None:
    """Publication encodes without decoding, so the encoder enforces the readers' ceiling."""
    overhead = len(canonical_json_bytes({"pad": ""}))
    at_ceiling = value_type({"pad": "x" * (ceiling - overhead)})
    assert len(encoder(at_ceiling)) == ceiling
    over_ceiling = value_type({"pad": "x" * (ceiling - overhead + 1)})
    with pytest.raises(SimulationCampaignIntegrityError, match="size ceiling"):
        encoder(over_ceiling)


def test_codec_rejects_noncanonical_or_oversized_json() -> None:
    raw = json.dumps(_attempt()).encode()
    with pytest.raises(SimulationCampaignIntegrityError, match="canonical"):
        decode_simulation_attempt(raw)
    with pytest.raises(SimulationCampaignIntegrityError, match="size ceiling"):
        decode_simulation_attempt(b" " * (MANIFEST_MAX_BYTES + 1))


@given(st.sampled_from(sorted(_attempt())))
def test_attempt_missing_field_mutation_is_rejected(field: str) -> None:
    mutated = _attempt()
    del mutated[field]
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(mutated))


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20))
def test_attempt_unknown_field_mutation_is_rejected(field: str) -> None:
    if field in _attempt():
        return
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(_attempt() | {field: None}))


@given(st.sampled_from(["", "/root", "../escape", "a/../b", "a//b", "C:/run", "a\\b"]))
def test_campaign_paths_reject_traversal_and_nonportable_forms(path: str) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        validate_relative_path(path)


@given(
    st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12),
        min_size=1,
        max_size=8,
        unique=True,
    )
)
def test_campaign_paths_accept_normalized_components(parts: list[str]) -> None:
    safe = [part.replace("/", "_").replace("\\", "_") for part in parts]
    if any(part in {"", ".", ".."} or ":" in part for part in safe):
        return
    path = "/".join(safe)
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
