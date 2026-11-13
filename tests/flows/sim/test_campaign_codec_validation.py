"""Focused rejection tests for campaign documents at the persistence boundary."""

from __future__ import annotations

import json

import pytest

from booley.flows.sim.campaign import codec
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from tests.flows.sim.test_campaign_codec_golden import (
    _BUILD_READY,
    _simulation_result,
)
from tests.flows.sim.test_campaign_manifest_codec import _manifest
from tests.flows.sim.test_campaign_model import _attempt


@pytest.mark.parametrize(
    "value",
    [
        {"x": object()},
        ["x" * (codec.MAX_COMMAND_BYTES + 1)],
        [None] * (codec.MAX_LIST_ITEMS + 1),
    ],
)
def test_json_resource_validation_rejects_unsupported_or_oversized_values(value: object) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        codec._validate_json(value)


def test_json_resource_validation_rejects_excessive_depth() -> None:
    value: object = None
    for _ in range(codec.MAX_JSON_DEPTH + 2):
        value = [value]
    with pytest.raises(SimulationCampaignIntegrityError, match="nesting"):
        codec._validate_json(value)


@pytest.mark.parametrize(
    ("function", "value"),
    [
        (codec._require_digest, 1),
        (codec._require_digest, "sha256:bad"),
        (codec._require_uuid, 1),
        (codec._require_uuid, "550e8400-e29b-11d4-a716-446655440000"),
    ],
)
def test_scalar_contract_helpers_reject_invalid_values(function, value: object) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        function(value, "field")


def test_codec_rejects_non_json_wrong_shape_and_wrong_schema() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="canonical JSON"):
        codec.canonical_json_bytes({"value": object()})
    with pytest.raises(SimulationCampaignIntegrityError):
        codec.decode_bundle_build_result(codec.canonical_json_bytes([]))
    document = json.loads(_BUILD_READY)
    document["$schema"] = "unknown"
    with pytest.raises(SimulationCampaignIntegrityError, match="unsupported"):
        codec.decode_bundle_build_result(codec.canonical_json_bytes(document))


def test_common_timestamps_must_be_canonical_utc() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="RFC3339"):
        codec._validate_common({"created_at": "not-a-date"})
    with pytest.raises(SimulationCampaignIntegrityError, match="RFC3339"):
        codec._validate_common({"created_at": "2026-09-21T10:00:00.1Z"})


def test_encoder_and_path_type_guards() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="expected"):
        codec.encode_simulation_result(codec.decode_bundle_build_result(_BUILD_READY))  # type: ignore[arg-type]
    with pytest.raises(SimulationCampaignIntegrityError, match="unsupported"):
        codec.encode_simulation_campaign_document(object())  # type: ignore[arg-type]
    with pytest.raises(SimulationCampaignIntegrityError, match="1 KiB"):
        codec.validate_relative_path("x" * (codec.MAX_PATH_BYTES + 1))


def test_additional_scalar_and_collection_ceilings() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="UUIDv4"):
        codec._require_uuid("bad", "field")
    with pytest.raises(SimulationCampaignIntegrityError, match="invalid"):
        codec._require_variant_id("variant:bad")
    with pytest.raises(SimulationCampaignIntegrityError, match="capped"):
        codec._exact_list([None, None], "values", 1)


def _fixture_document(name: str) -> dict[str, object]:
    from pathlib import Path

    files = Path(__file__).parents[1] / "fixtures/simulation_campaign"
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


def _tamper_snapshot(document: dict[str, object]) -> None:
    document["inventory_sha256"] = "sha256:" + "f" * 64
    document["artifacts"][0]["kind"] = "runtime_input"  # type: ignore[index]


@pytest.mark.parametrize(
    ("decode", "make_document", "tamper"),
    [
        (codec.decode_simulation_attempt, _attempt, _tamper_attempt),
        (
            codec.decode_bundle_build_attempt,
            lambda: _fixture_document("build-attempt-private.json"),
            _tamper_build_attempt,
        ),
        (codec.decode_bundle_build_result, lambda: json.loads(_BUILD_READY), _tamper_build_result),
        (
            codec.decode_simulator_bundle,
            lambda: _fixture_document("bundle.json"),
            _tamper_bundle,
        ),
        (
            codec.decode_executable_snapshot,
            lambda: _fixture_document("snapshot.json"),
            _tamper_snapshot,
        ),
        (
            codec.decode_simulation_result,
            lambda: json.loads(_simulation_result("completed")),
            _tamper_result,
        ),
    ],
    ids=["attempt", "build_attempt", "build_result", "bundle", "snapshot", "result"],
)
def test_record_decoders_do_not_revalidate_nested_fields(decode, make_document, tamper) -> None:
    """These records get only the boundary check; their consumers guard what they use (#1743)."""
    document = make_document()
    tamper(document)
    raw = codec.canonical_json_bytes(document)
    assert decode(raw).canonical_bytes() == raw


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
@pytest.mark.parametrize(
    "reader",
    ["manifest", "attempt", "build_attempt", "build_result", "bundle", "snapshot", "result"],
)
def test_reader_rejects_corrupt_record(tmp_path, reader, error):
    from pathlib import Path

    from tests.flows.sim.test_campaign_test_runs import _result_document

    files = Path(__file__).parents[1] / "fixtures/simulation_campaign"
    readers = {
        "manifest": (codec.decode_simulation_campaign_manifest, _manifest),
        "attempt": (codec.decode_simulation_attempt, _attempt),
        "build_attempt": (
            codec.decode_bundle_build_attempt,
            lambda: json.loads((files / "build-attempt-private.json").read_bytes()),
        ),
        "build_result": (codec.decode_bundle_build_result, lambda: json.loads(_BUILD_READY)),
        "bundle": (
            codec.decode_simulator_bundle,
            lambda: json.loads((files / "bundle.json").read_bytes()),
        ),
        "snapshot": (
            codec.decode_executable_snapshot,
            lambda: json.loads((files / "snapshot.json").read_bytes()),
        ),
        "result": (codec.decode_simulation_result, lambda: _result_document(tmp_path)),
    }
    decode, make_document = readers[reader]
    document = make_document()
    decode(codec.canonical_json_bytes(document))
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document[next(key for key in document if key != "$schema")]
    raw = b"{" if error == "truncated" else codec.canonical_json_bytes(document)
    with pytest.raises(SimulationCampaignIntegrityError):
        decode(raw)


def test_manifest_decode_does_not_revalidate_planned_sections() -> None:
    document = _manifest()
    workload = document["workload"]
    workload["mode"] = "lint"
    workload["source_recipe"]["defines"] = [""]
    workload["build_recipe"]["toplevel"] = ""
    document["required_suite"] = {"names": ["x", "x"], "default_invocation": False}
    document["build_variants"][0]["kind"] = "unknown"
    document["planning_disclosures"] = [{"planner": ""}]
    document["origin"]["execution_id"] = "not-an-execution"
    document["target"]["role"] = "unknown"
    document["target"]["display_name"] = 7
    item = document["work_items"][0]
    item["kind"] = "unknown"
    item["selection"]["kind"] = "unknown"
    item["run_directory"]["collision_template"] = "../escape"
    item["arguments"] = [1]
    item["fingerprint_sha256"] = "not-a-digest"
    document["fingerprints"]["unexpected"] = "not-a-digest"
    raw = codec.canonical_json_bytes(document)
    assert codec.decode_simulation_campaign_manifest(raw).canonical_bytes() == raw


def test_manifest_reader_rejects_a_wrong_nested_type() -> None:
    document = _manifest()
    document["workload"]["run_cwd"]["configured"] = 7
    raw = codec.canonical_json_bytes(document)
    with pytest.raises(SimulationCampaignIntegrityError, match=r"run_cwd\.configured"):
        codec.decode_simulation_campaign_manifest(raw)


def _prerequisite_entry() -> dict[str, object]:
    return {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": "targets/base/record/manifest.json",
            "bytes": 1,
            "sha256": "sha256:" + "a" * 64,
            "kind": "simulation_campaign_manifest",
            "owner": "550e8400-e29b-41d4-a716-446655440000",
        },
        "campaign_id": "550e8400-e29b-41d4-a716-446655440000",
        "target": {**_manifest()["target"], "role": "cycle_count_baseline"},
        "required_observation": "cycle_count",
        "work_item_id": "item:0000:0123456789abcdef",
    }


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (("workload", "run_cwd", "configured"), r"run_cwd\.configured"),
        (("target", "selector"), r"target\.selector"),
        (("work_items", 0, "kind"), r"work_items\[0\]\.kind"),
        (
            ("prerequisites", 0, "manifest", "path"),
            r"prerequisites\[0\]\.manifest\.path",
        ),
    ],
    ids=["workload-run-cwd", "target-selector", "work-item-kind", "prerequisite-manifest-path"],
)
def test_manifest_reader_rejects_a_missing_nested_field(tmp_path, path, field) -> None:
    from booley.flows.sim.campaign.inspection import inspect_retained_campaign

    document = _manifest()
    document["prerequisites"] = [_prerequisite_entry()]
    parent: object = document
    for part in path[:-1]:
        parent = parent[part]  # type: ignore[index]
    del parent[path[-1]]  # type: ignore[attr-defined,arg-type]
    raw = codec.canonical_json_bytes(document)
    with pytest.raises(SimulationCampaignIntegrityError, match=field):
        codec.decode_simulation_campaign_manifest(raw)
    manifest = tmp_path / "record" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_bytes(raw)
    with pytest.raises(SimulationCampaignIntegrityError, match=field):
        inspect_retained_campaign(manifest)
