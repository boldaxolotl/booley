from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booley.flows.sim.campaign.codec import (
    MANIFEST_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_executable_snapshot,
    encode_simulation_attempt,
    encode_simulator_bundle,
    validate_relative_path,
)
from booley.flows.sim.campaign.model import (
    AssertionObservation,
    ExecutableSnapshot,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
    SimulatorBundle,
    grade_observations,
)


def _attempt() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-attempt/v1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
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


def test_directly_constructed_documents_deep_freeze_and_validate_on_write() -> None:
    source = _attempt()
    value = SimulationAttempt(source)
    source["run_directory"]["owned"] = False  # type: ignore[index]
    assert value.document["run_directory"]["owned"] is True  # type: ignore[index]
    with pytest.raises(TypeError):
        value.document["run_directory"]["owned"] = False  # type: ignore[index]

    invalid = SimulationAttempt(_attempt() | {"attempt_ordinal": 0})
    with pytest.raises(SimulationCampaignIntegrityError, match="positive integer"):
        encode_simulation_attempt(invalid)


def test_attempt_golden_fixture_is_independent_literal_bytes() -> None:
    fixture = (
        Path(__file__).parents[1] / "fixtures/simulation_campaign/attempt.json"
    ).read_bytes()
    assert decode_simulation_attempt(fixture).canonical_bytes() == fixture


@pytest.mark.parametrize(
    ("name", "decoder"),
    [
        ("attempt-child.json", decode_simulation_attempt),
        ("build-attempt-shared.json", decode_bundle_build_attempt),
        ("build-attempt-private.json", decode_bundle_build_attempt),
        ("bundle.json", decode_simulator_bundle),
        ("snapshot.json", decode_executable_snapshot),
    ],
)
def test_attempt_branch_golden_fixtures_are_literal_bytes(name, decoder) -> None:
    fixture = (Path(__file__).parents[1] / "fixtures/simulation_campaign" / name).read_bytes()
    assert decoder(fixture).canonical_bytes() == fixture


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
    raw = (Path(__file__).parents[1] / "fixtures/simulation_campaign" / name).read_bytes()
    assert encoder(decoder(raw)) == raw


def test_attempt_codec_rejects_unknown_fields_and_bool_integer() -> None:
    unknown = _attempt() | {"surprise": True}
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        decode_simulation_attempt(canonical_json_bytes(unknown))
    invalid = _attempt()
    invalid["attempt_ordinal"] = True
    with pytest.raises(SimulationCampaignIntegrityError, match="positive integer"):
        decode_simulation_attempt(canonical_json_bytes(invalid))


@pytest.mark.parametrize("ordinal", [10_001, 1_000_000])
def test_attempt_codec_enforces_attempt_resource_ceiling(ordinal: int) -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="attempt ceiling"):
        decode_simulation_attempt(canonical_json_bytes(_attempt() | {"attempt_ordinal": ordinal}))


def test_build_attempt_codec_enforces_attempt_resource_ceiling() -> None:
    path = (
        Path(__file__).parents[1]
        / "fixtures/simulation_campaign/build-attempt-shared.json"
    )
    document = json.loads(path.read_bytes())
    document["build_attempt_ordinal"] = 10_001
    with pytest.raises(SimulationCampaignIntegrityError, match="attempt ceiling"):
        decode_bundle_build_attempt(canonical_json_bytes(document))


@pytest.mark.parametrize("field", ["configured", "resolved", "collision_key"])
def test_attempt_codec_rejects_non_string_run_directory_fields(field: str) -> None:
    attempt = _attempt()
    attempt["run_directory"][field] = 1  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(attempt))


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


@given(st.sampled_from([True, False, 0, -1, 1.5, "1", None]))
def test_attempt_integer_type_mutation_is_rejected(value: object) -> None:
    mutated = _attempt()
    mutated["attempt_ordinal"] = value
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(mutated))


@given(st.text(alphabet="0123456789abcdef", min_size=0, max_size=70))
def test_attempt_digest_mutation_is_rejected(digest: str) -> None:
    if len(digest) == 64:
        return
    mutated = _attempt()
    mutated["manifest_sha256"] = "sha256:" + digest
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_attempt(canonical_json_bytes(mutated))


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


@given(
    st.sampled_from(["missing", "unknown", "type", "path", "digest", "conditional"]),
    st.sampled_from(["bundle.json", "snapshot.json"]),
)
def test_bundle_and_snapshot_structural_mutations_are_rejected(
    mutation: str, name: str
) -> None:
    path = Path(__file__).parents[1] / "fixtures/simulation_campaign" / name
    document = json.loads(path.read_bytes())
    decoder = decode_simulator_bundle if name == "bundle.json" else decode_executable_snapshot
    artifact = document["artifacts"][0]
    if mutation == "missing":
        del artifact["sha256"]
    elif mutation == "unknown":
        artifact["unexpected"] = True
    elif mutation == "type":
        artifact["bytes"] = True
    elif mutation == "path":
        artifact["path"] = "../escape"
    elif mutation == "digest":
        document["inventory_sha256"] = "sha256:" + "f" * 64
    elif name == "bundle.json":
        document["sharing"] = "private_work_item"
    else:
        artifact["kind"] = "runtime_input"
    with pytest.raises(SimulationCampaignIntegrityError):
        decoder(canonical_json_bytes(document))
    value_type = SimulatorBundle if name == "bundle.json" else ExecutableSnapshot
    encoder = encode_simulator_bundle if name == "bundle.json" else encode_executable_snapshot
    with pytest.raises(SimulationCampaignIntegrityError):
        encoder(value_type(document))


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
