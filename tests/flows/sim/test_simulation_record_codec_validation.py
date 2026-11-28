"""Focused rejection tests for record documents at the persistence boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.flows.sim.record import codec
from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from tests.flows.sim.test_simulation_record_codec_golden import _BUILD_READY, _simulation_result
from tests.flows.sim.test_simulation_record_manifest_codec import _manifest
from tests.flows.sim.test_simulation_record_model import _attempt


def test_codec_rejects_non_json_wrong_shape_and_wrong_schema() -> None:
    with pytest.raises(SimulationRecordIntegrityError, match="canonical JSON"):
        codec.canonical_json_bytes({"value": object()})
    with pytest.raises(SimulationRecordIntegrityError):
        codec.decode_bundle_build_result(codec.canonical_json_bytes([]))
    document = json.loads(_BUILD_READY)
    document["$schema"] = "unknown"
    with pytest.raises(SimulationRecordIntegrityError, match="unsupported"):
        codec.decode_bundle_build_result(codec.canonical_json_bytes(document))


def test_encoder_and_path_type_guards() -> None:
    with pytest.raises(SimulationRecordIntegrityError, match="expected"):
        codec.encode_simulation_result(codec.decode_bundle_build_result(_BUILD_READY))  # type: ignore[arg-type]
    with pytest.raises(SimulationRecordIntegrityError, match="unsupported"):
        codec.encode_simulation_record_document(object())  # type: ignore[arg-type]
    with pytest.raises(SimulationRecordIntegrityError, match="1 KiB"):
        codec.validate_relative_path("x" * (codec.MAX_PATH_BYTES + 1))


def _fixture_document(name: str) -> dict[str, object]:
    files = Path(__file__).parents[1] / "fixtures/simulation_record"
    return json.loads((files / name).read_bytes())


def _tamper_attempt(document: dict[str, object]) -> None:
    document["pre_sim_build_access"] = "mutable"
    document["attempt_ordinal"] = 10_001
    document["run_directory"]["kind"] = "unknown"  # type: ignore[index]
    document["child_execution_id"] = None
    document["child_entry_sha256"] = "not-a-digest"


def _tamper_build_attempt(document: dict[str, object]) -> None:
    document["sharing"] = "invalid"
    document["owner"]["simulation_attempt_id"] = None  # type: ignore[index]


def _tamper_build_result(document: dict[str, object]) -> None:
    document["state"] = "design_failure"
    document["bundle"]["artifacts"][0]["path"] = "../escape"  # type: ignore[index]


def _tamper_bundle(document: dict[str, object]) -> None:
    document["inventory_sha256"] = "sha256:" + "f" * 64
    document["artifacts"][0]["kind"] = "log"  # type: ignore[index]


def _tamper_result(document: dict[str, object]) -> None:
    document["state"] = "unknown"
    document["grade"] = "unknown"
    document["build_result"]["path"] = "../escape"  # type: ignore[index]
    document["observations"][0]["execution"] = "unknown"  # type: ignore[index]
    document["diagnostics"] = [{}]


def _tamper_manifest(document: dict[str, object]) -> None:
    workload = document["workload"]
    workload["mode"] = "lint"  # type: ignore[index]
    workload["source_recipe"]["defines"] = [""]  # type: ignore[index]
    workload["build_recipe"]["toplevel"] = ""  # type: ignore[index]
    document["required_suite"] = {"names": ["x", "x"], "default_invocation": False}
    document["build_variants"][0]["kind"] = "unknown"  # type: ignore[index]
    document["build_variants"][0]["recipe_sha256"] = "sha256:" + "1" * 64  # type: ignore[index]
    document["planning_disclosures"] = [{"planner": ""}]
    document["origin"]["execution_id"] = "not-an-execution"  # type: ignore[index]
    document["target"]["role"] = "unknown"  # type: ignore[index]
    document["target"]["display_name"] = 7  # type: ignore[index]
    item = document["work_items"][0]  # type: ignore[index]
    item["kind"] = "unknown"
    item["selection"]["kind"] = "unknown"
    item["run_directory"]["collision_template"] = "../escape"
    item["arguments"] = [1]
    item["revision"] = "other-revision"
    item["fingerprint_sha256"] = "not-a-digest"
    item["test_runs"][0]["fingerprint"] = "sha256:" + "3" * 64
    document["fingerprints"]["unexpected"] = "not-a-digest"  # type: ignore[index]
    document["fingerprints"]["workload_sha256"] = "sha256:" + "2" * 64  # type: ignore[index]
    document["simulation_record_id"] = "not-a-uuid"
    document["created_at"] = "yesterday"
    document["target"]["selector"] = 7  # type: ignore[index]
    document["extra_field"] = None


@pytest.mark.parametrize(
    ("decode", "make_document", "tamper"),
    [
        (codec.decode_simulation_record_manifest, _manifest, _tamper_manifest),
        (codec.decode_simulation_attempt, _attempt, _tamper_attempt),
        (
            codec.decode_bundle_build_attempt,
            lambda: _fixture_document("build-attempt-private.json"),
            _tamper_build_attempt,
        ),
        (codec.decode_bundle_build_result, lambda: json.loads(_BUILD_READY), _tamper_build_result),
        (codec.decode_simulator_bundle, lambda: _fixture_document("bundle.json"), _tamper_bundle),
        (
            codec.decode_simulation_result,
            lambda: json.loads(_simulation_result("completed")),
            _tamper_result,
        ),
    ],
    ids=["manifest", "attempt", "build_attempt", "build_result", "bundle", "result"],
)
def test_record_decoders_do_not_revalidate_nested_fields(decode, make_document, tamper) -> None:
    """These records get only the boundary check; their consumers guard what they use (#1743)."""
    document = make_document()
    tamper(document)
    raw = json.dumps(document, indent=1).encode()  # not canonical
    assert decode(raw).canonical_bytes() == codec.canonical_json_bytes(document)


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
def test_reader_rejects_corrupt_record(error):
    """All six readers share one decode path, so the manifest reader stands for them."""
    document = _manifest()
    codec.decode_simulation_record_manifest(codec.canonical_json_bytes(document))
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document[next(key for key in document if key != "$schema")]
    raw = b"{" if error == "truncated" else codec.canonical_json_bytes(document)
    with pytest.raises(SimulationRecordIntegrityError):
        codec.decode_simulation_record_manifest(raw)
