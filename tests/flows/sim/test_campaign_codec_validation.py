"""Focused rejection tests for campaign documents at the persistence boundary."""

from __future__ import annotations

import json

import pytest

from booley.flows.sim.campaign import codec
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from tests.flows.sim.test_campaign_codec_golden import (
    _BUILD_DESIGN,
    _BUILD_READY,
    _simulation_result,
)
from tests.flows.sim.test_campaign_manifest_codec import _manifest
from tests.flows.sim.test_campaign_model import _attempt


def _reject_build(path: tuple[str | int, ...], value: object) -> None:
    document = json.loads(_BUILD_READY)
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        codec.decode_build_result(codec.canonical_json_bytes(document))


def _reject_result(path: tuple[str | int, ...], value: object, state: str = "completed") -> None:
    document = json.loads(_simulation_result(state))
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("elapsed_seconds",), -1),
        (("state",), "unknown"),
        (("phase",), "compile"),
        (("build_attempt", "kind"), "log"),
        (("build_attempt", "owner"), "550e8400-e29b-41d4-a716-446655440001"),
        (("bundle", "sharing"), "private"),
        (("bundle", "artifacts", 0, "kind"), "log"),
        (("bundle", "artifacts", 0, "bytes"), -1),
    ],
)
def test_build_result_rejects_invalid_contract_values(
    path: tuple[str | int, ...], value: object
) -> None:
    _reject_build(path, value)


def test_build_result_rejects_duplicate_or_nonexecutable_artifacts() -> None:
    document = json.loads(_BUILD_READY)
    artifact = document["bundle"]["artifacts"][0]
    document["bundle"]["artifacts"] = [artifact, dict(artifact)]
    with pytest.raises(SimulationCampaignIntegrityError, match="unique"):
        codec.decode_build_result(codec.canonical_json_bytes(document))

    document["bundle"]["artifacts"] = [{**artifact, "kind": "runtime_data"}]
    with pytest.raises(SimulationCampaignIntegrityError, match="simulator executable"):
        codec.decode_build_result(codec.canonical_json_bytes(document))


def test_build_result_rejects_observation_disagreements() -> None:
    document = json.loads(_BUILD_DESIGN)
    document["observation"]["class"] = "infrastructure"
    with pytest.raises(SimulationCampaignIntegrityError, match="disagrees"):
        codec.decode_build_result(codec.canonical_json_bytes(document))

    document = json.loads(_BUILD_DESIGN)
    document["observation"]["class"] = "unknown"
    with pytest.raises(SimulationCampaignIntegrityError, match="class is invalid"):
        codec.decode_build_result(codec.canonical_json_bytes(document))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("state",), "unknown"),
        (("grade",), "unknown"),
        (("build_result", "kind"), "log"),
        (("build_result", "state"), "unknown"),
        (("build_result", "sharing"), "unknown"),
        (("build_result", "owner"), "550e8400-e29b-41d4-a716-446655440001"),
        (("executable_snapshot", "verified_after_exit"), False),
        (("executable_snapshot", "post_exit_sha256"), "sha256:" + "f" * 64),
        (("executable_snapshot", "manifest", "kind"), "log"),
        (("observations", 0, "execution"), "unknown"),
        (("observations", 0, "failure_class"), "unknown"),
        (("observations", 0, "functional"), "unknown"),
        (("observations", 0, "assertions"), "unknown"),
        (("observations", 0, "assertion_count"), -1),
        (("observations", 0, "cycle_count"), -1),
    ],
)
def test_simulation_result_rejects_invalid_contract_values(
    path: tuple[str | int, ...], value: object
) -> None:
    _reject_result(path, value)


def test_simulation_result_rejects_state_and_observation_disagreements() -> None:
    document = json.loads(_simulation_result("completed"))
    document["observations"] = []
    with pytest.raises(SimulationCampaignIntegrityError, match="nonempty"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))

    document = json.loads(_simulation_result("completed"))
    document["observations"][0]["execution"] = "timeout"
    with pytest.raises(SimulationCampaignIntegrityError, match="completed observations"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))

    document = json.loads(_simulation_result("completed"))
    document["observations"].append(dict(document["observations"][0]))
    with pytest.raises(SimulationCampaignIntegrityError, match="unique"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))

    document = json.loads(_simulation_result("blocked_by_build"))
    document["bundle_id"] = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with pytest.raises(SimulationCampaignIntegrityError, match="cannot bind"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))

    document = json.loads(_simulation_result("blocked_by_build"))
    document["build_result"]["state"] = "ready"
    with pytest.raises(SimulationCampaignIntegrityError, match="failed build"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))


def test_simulation_result_rejects_diagnostic_and_matrix_errors() -> None:
    document = json.loads(_simulation_result("completed"))
    document["diagnostics"] = [{"severity": "info", "code": "x", "pointer": "", "message": "bad"}]
    with pytest.raises(SimulationCampaignIntegrityError, match="severity"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))

    document = json.loads(_simulation_result("setup_error"))
    document["observations"][0]["functional"] = "pass"
    with pytest.raises(SimulationCampaignIntegrityError, match="matrix"):
        codec.decode_simulation_result(codec.canonical_json_bytes(document))


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
        (codec._require_positive_int, False),
        (codec._require_positive_int, 0),
        (codec._require_nonnegative_int, False),
        (codec._require_nonnegative_int, -1),
        (codec._require_digest, 1),
        (codec._require_digest, "sha256:bad"),
        (codec._require_uuid, 1),
        (codec._require_uuid, "550e8400-e29b-11d4-a716-446655440000"),
    ],
)
def test_scalar_contract_helpers_reject_invalid_values(function, value: object) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        function(value, "field")


@pytest.mark.parametrize(
    ("configured", "kind"),
    [
        ("run/{unknown}", "templated"),
        ("run/{test!r}", "templated"),
        ("run/{test:10}", "templated"),
        ("run/{test", "templated"),
        ("run/{test}", "literal"),
    ],
)
def test_run_directory_format_rejects_ambiguous_templates(configured: str, kind: str) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        codec._validate_configured_run_kind(configured, kind)


def test_manifest_rejects_invalid_top_level_contracts() -> None:
    invalid_documents: list[dict[str, object]] = []
    for path, value in [
        (("origin", "execution_id"), "not-an-execution"),
        (("origin", "invocation_id"), 0),
        (("target", "role"), "unknown"),
        (("workload", "mode"), "lint"),
        (("workload", "pre_sim_build_access"), "mutable"),
        (("workload", "run_cwd", "kind"), "unknown"),
        (("workload", "run_cwd", "placeholders"), ["unknown"]),
        (("required_suite", "default_invocation"), False),
        (("build_variants", 0, "kind"), "unknown"),
        (("work_items", 0, "ordinal"), 2),
        (("work_items", 0, "kind"), "unknown"),
        (("work_items", 0, "role"), "unknown"),
        (("work_items", 0, "selection", "kind"), "unknown"),
        (("work_items", 0, "run_directory", "kind"), "unknown"),
        (("work_items", 0, "run_directory", "collision_template"), "../escape"),
    ]:
        document = _manifest()
        target: object = document
        for part in path[:-1]:
            target = target[part]  # type: ignore[index]
        target[path[-1]] = value  # type: ignore[index]
        invalid_documents.append(document)

    for document in invalid_documents:
        with pytest.raises((SimulationCampaignIntegrityError, TypeError, ValueError)):
            codec.decode_simulation_campaign_manifest(codec.canonical_json_bytes(document))


def test_codec_rejects_non_json_wrong_shape_and_wrong_schema() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="canonical JSON"):
        codec.canonical_json_bytes({"value": object()})
    with pytest.raises(SimulationCampaignIntegrityError):
        codec.decode_build_result(codec.canonical_json_bytes([]))
    document = json.loads(_BUILD_READY)
    document["$schema"] = "unknown"
    with pytest.raises(SimulationCampaignIntegrityError, match="unsupported"):
        codec.decode_build_result(codec.canonical_json_bytes(document))


def test_nested_json_string_validation_walks_lists_objects_and_keys() -> None:
    codec._validate_bounded_json_strings({"key": ["value"]}, "detail")
    with pytest.raises(SimulationCampaignIntegrityError, match="ceiling"):
        codec._validate_bounded_json_strings(
            {"key": ["x" * (codec.MAX_STRING_BYTES + 1)]}, "detail"
        )


def test_common_timestamps_must_be_canonical_utc() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="RFC3339"):
        codec._validate_common({"created_at": "not-a-date"})
    with pytest.raises(SimulationCampaignIntegrityError, match="RFC3339"):
        codec._validate_common({"created_at": "2026-09-21T10:00:00.1Z"})


def test_child_and_build_attempt_identities_must_be_coherent() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="both"):
        codec._validate_child_identity(
            {"child_execution_id": "e" * 32, "child_entry_sha256": None}
        )
    with pytest.raises(SimulationCampaignIntegrityError, match="32 lowercase"):
        codec._validate_child_identity(
            {
                "child_execution_id": "bad",
                "child_entry_sha256": "sha256:" + "1" * 64,
            }
        )

    base = {
        "build_attempt_ordinal": 1,
        "producer_invocation_id": 1,
        "build_attempt_id": "550e8400-e29b-41d4-a716-446655440000",
        "build_variant_id": "variant:" + "1" * 64,
        "owner": {"work_item_id": None, "simulation_attempt_id": None},
        "tool_provenance": {
            "eda_kind": "icarus",
            "eda_version": "12",
            "adapter_contract_version": "1",
        },
        "sharing": "shared_variant",
    }
    invalid = dict(base, sharing="invalid")
    with pytest.raises(SimulationCampaignIntegrityError, match="sharing"):
        codec._validate_build_attempt(invalid)
    invalid = dict(
        base, owner={"work_item_id": "item:0000:0123456789abcdef", "simulation_attempt_id": None}
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="both"):
        codec._validate_build_attempt(invalid)


def test_runtime_declarations_require_digest_and_unique_identity() -> None:
    declaration = {
        "source_artifact_path": "input/a",
        "destination": "run/a",
        "declaration_id": "sha256:" + "0" * 64,
    }
    with pytest.raises(SimulationCampaignIntegrityError, match="digest"):
        codec._validate_runtime_declarations([declaration])
    declaration["declaration_id"] = codec._digest(
        {"source_artifact_path": "input/a", "destination": "run/a"}
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="unique"):
        codec._validate_runtime_declarations([declaration, declaration])


def test_source_recipe_rejects_duplicate_parameters_and_invalid_sources() -> None:
    recipe = {
        "sources": [],
        "parameters": [{"name": "WIDTH", "value": {"nested": [1]}}] * 2,
        "defines": [],
        "pre_sim_commands": [],
    }
    with pytest.raises(SimulationCampaignIntegrityError, match="unique"):
        codec._validate_source_recipe(recipe)
    with pytest.raises(SimulationCampaignIntegrityError, match="kind"):
        codec._validate_source_entries(
            [
                {
                    "path": "rtl/top.sv",
                    "bytes": 1,
                    "sha256": "sha256:" + "1" * 64,
                    "kind": "unknown",
                }
            ],
            "sources",
        )


@pytest.mark.parametrize(
    "suite",
    [
        {
            "names": ["test"],
            "default_invocation": True,
            "source_path": "tests.txt",
            "source_bytes": 1,
            "source_sha256": "sha256:" + "1" * 64,
        },
        {
            "names": [],
            "default_invocation": True,
            "source_path": "tests.txt",
            "source_bytes": 1,
            "source_sha256": "sha256:" + "1" * 64,
        },
    ],
)
def test_required_suite_rejects_default_invocation_payloads(suite) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        codec._validate_required_suite(suite)


@pytest.mark.parametrize(
    ("selection", "kind"),
    [
        ({"kind": "default", "names": ["test"]}, "ordinary_hdl"),
        ({"kind": "named", "names": ["a", "b"]}, "ordinary_hdl"),
        ({"kind": "unfiltered", "names": []}, "ordinary_hdl"),
        ({"kind": "default", "names": []}, "cocotb_batch"),
        ({"kind": "default", "names": []}, "coverage_aggregate"),
    ],
)
def test_work_item_selection_rejects_incompatible_shapes(selection, kind: str) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        codec._validate_work_item_selection(selection, kind)


def test_encoder_and_path_type_guards() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="expected"):
        codec.encode_simulation_result(codec.decode_build_result(_BUILD_READY))  # type: ignore[arg-type]
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


def _runtime_binding() -> dict[str, object]:
    digest = "sha256:" + "1" * 64
    return {
        "declaration_id": "sha256:" + "2" * 64,
        "authoritative_copy": {
            "path": "inputs/a",
            "bytes": 1,
            "sha256": digest,
            "kind": "runtime_input",
            "owner": "550e8400-e29b-41d4-a716-446655440001",
        },
        "destination": "run/a",
        "method": "copy",
        "destination_bytes": 1,
        "destination_sha256": digest,
        "owned": True,
    }


def test_runtime_inputs_reject_blocked_invalid_mismatched_and_duplicate_bindings() -> None:
    binding = _runtime_binding()
    with pytest.raises(SimulationCampaignIntegrityError, match="must be empty"):
        codec._validate_runtime_inputs([binding], blocked=True)

    invalid = dict(binding, method="move")
    with pytest.raises(SimulationCampaignIntegrityError, match="method"):
        codec._validate_runtime_inputs([invalid], blocked=False)

    invalid = dict(binding, destination_bytes=2)
    with pytest.raises(SimulationCampaignIntegrityError, match="disagree"):
        codec._validate_runtime_inputs([invalid], blocked=False)

    with pytest.raises(SimulationCampaignIntegrityError, match="unique"):
        codec._validate_runtime_inputs([binding, binding], blocked=False)


def test_observations_reject_state_grade_cycle_and_diagnostic_disagreements() -> None:
    completed = json.loads(_simulation_result("completed"))["observations"][0]
    blocked = json.loads(_simulation_result("blocked_by_build"))["observations"][0]
    setup = json.loads(_simulation_result("setup_error"))["observations"][0]
    with pytest.raises(SimulationCampaignIntegrityError, match="blocked observations"):
        codec._validate_observations([completed], "blocked_by_build")
    with pytest.raises(SimulationCampaignIntegrityError, match="setup observations"):
        codec._validate_observations([completed], "setup_error")
    with pytest.raises(SimulationCampaignIntegrityError, match="state disagrees"):
        codec._validate_observations([completed], "timeout")
    with pytest.raises(SimulationCampaignIntegrityError, match="grade disagrees"):
        codec._validate_result_grade("fail", [completed])

    invalid_cycle = dict(blocked, cycle_count=1)
    with pytest.raises(SimulationCampaignIntegrityError, match="cycle count"):
        codec._validate_observation(invalid_cycle, 0)

    codec._validate_diagnostics(
        [{"severity": "warning", "code": "warn", "pointer": "", "message": "message"}]
    )
    assert setup["execution"] == "setup_error"


def test_run_cwd_declared_placeholders_must_match_template() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="disagree"):
        codec._validate_run_cwd(
            {"configured": "run/{test}", "kind": "templated", "placeholders": ["attempt"]}
        )


def test_simulation_attempt_rejects_access_and_directory_kinds() -> None:
    document = _attempt()
    document["pre_sim_build_access"] = "mutable"
    with pytest.raises(SimulationCampaignIntegrityError, match="build_access"):
        codec.decode_simulation_attempt(codec.canonical_json_bytes(document))

    document = _attempt()
    document["run_directory"]["kind"] = "unknown"  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="directory kind"):
        codec.decode_simulation_attempt(codec.canonical_json_bytes(document))
