from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from booley.sim.coverage_campaign import (
    CoverageCampaignValidationError,
    DurableTargetIdentity,
    decode_coverage_campaign,
    encode_coverage_campaign,
)

_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEwOmJhc2ljLWJsb2NrLTAiLCJyZWNvcm"
    "RfdHlwZSI6InZfbGluZSJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMH0sInNvdXJjZSI6InJ0bC9jb3VudGVyLnN2Ii"
    "wic3RhcnQiOnsiY29sdW1uIjozLCJsaW5lIjoxMH19LCJtZXRyaWMiOiJsaW5lIiwic3ViamVj"
    "dCI6eyJiYXNpY19ibG9jayI6MH19"
)
_TESTBENCH_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEwOmJhc2ljLWJsb2NrLTAiLCJyZWNvcm"
    "RfdHlwZSI6InZfbGluZSJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMH0sInNvdXJjZSI6InRiL2NvdW50ZXJfdGIuc3"
    "YiLCJzdGFydCI6eyJjb2x1bW4iOjMsImxpbmUiOjEwfX0sIm1ldHJpYyI6ImxpbmUiLCJzdWJq"
    "ZWN0Ijp7ImJhc2ljX2Jsb2NrIjowfX0"
)
_FSM_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEwOmZzbS1ydW4td3JhcCIsInJlY29yZF90eX"
    "BlIjoidl9mc20ifSwiaGllcmFyY2h5IjoiVE9QLmNvdW50ZXIiLCJsb2NhdGlvbiI6eyJlbmQiOnsi"
    "Y29sdW1uIjoyOCwibGluZSI6MTB9LCJzb3VyY2UiOiJydGwvY291bnRlci5zdiIsInN0YXJ0Ijp7Im"
    "NvbHVtbiI6MywibGluZSI6MTB9fSwibWV0cmljIjoiZnNtIiwic3ViamVjdCI6eyJtYWNoaW5lIjoi"
    "Y291bnRlcl9zdGF0ZSIsInRyYW5zaXRpb24iOiJSVU5fdG9fV1JBUCJ9fQ"
)
_BRANCH_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEyOmJyYW5jaC10cnVlIiwicmVjb3JkX3"
    "R5cGUiOiJ2X2JyYW5jaCJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMn0sInNvdXJjZSI6InJ0bC9jb3VudGVyLnN2Ii"
    "wic3RhcnQiOnsiY29sdW1uIjozLCJsaW5lIjoxMn19LCJtZXRyaWMiOiJicmFuY2giLCJzdWJq"
    "ZWN0Ijp7Im91dGNvbWUiOiJ0cnVlIn19"
)


def _fingerprint(character: str) -> str:
    return f"sha256:{character * 64}"


def _valid_provenance() -> dict[str, object]:
    return {
        "invocation": {"id": 12, "started_at": "2026-09-01T10:30:00Z"},
        "target": {"identity": "acme:demo:counter:1.0#sim_counter", "selector": "sim_counter"},
        "collector": {
            "kind": "verilator",
            "version": {"tag": "v5.046", "commit": "24b2ac24c721fdad89bba75a492e02c6aa63f32e"},
            "native_format": {"name": "verilator-coverage", "compatibility": "compatible"},
            "capabilities": [{"record_class": "line", "status": "reported"}],
        },
        "build": {
            "recipe_fingerprint": _fingerprint("5"),
            "instrumentation": ["--coverage-line", "--coverage-per-instance"],
            "trace": False,
            "coverage": True,
        },
        "coverage_window": {"mode": "whole_run", "hook_evidence_artifacts": []},
        "fingerprints": {
            "target_definition": _fingerprint("1"),
            "rtl_sources": _fingerprint("2"),
            "testbench_sources": _fingerprint("3"),
            "test_declarations": _fingerprint("4"),
            "instrumented_build": _fingerprint("5"),
        },
        "source_closure": {
            "rtl": [{"path": "rtl/counter.sv", "sha256": _fingerprint("6")}],
            "testbench": [{"path": "tb/counter_tb.sv", "sha256": _fingerprint("7")}],
        },
    }


def _valid_tests() -> dict[str, object]:
    return {
        "declared": ["reset"],
        "selected": ["reset"],
        "runs": [
            {
                "id": "run:reset",
                "test": "reset",
                "simulation_verdict": "pass",
                "collection": "included",
                "raw_artifact": "artifact:raw-reset",
            },
        ],
    }


def _valid_artifacts() -> list[dict[str, object]]:
    return [
        {
            "id": "artifact:raw-reset",
            "kind": "raw_native",
            "owner_run": "run:reset",
            "path": "native/raw/reset.dat",
            "sha256": _fingerprint("a"),
            "bytes": 4120,
            "state": "fresh_queryable",
        },
        {
            "id": "artifact:merged",
            "kind": "merged_native",
            "path": "native/merged/coverage.dat",
            "sha256": _fingerprint("b"),
            "bytes": 4250,
            "state": "fresh_queryable",
        },
    ]


def _valid_points() -> list[dict[str, object]]:
    return [
        {
            "id": _POINT_ID,
            "identity": {
                "metric": "line",
                "location": {
                    "source": "rtl/counter.sv",
                    "start": {"line": 10, "column": 3},
                    "end": {"line": 10, "column": 28},
                },
                "hierarchy": "TOP.counter",
                "subject": {"basic_block": 0},
                "collector": {"record_type": "v_line", "native_key": "10:basic-block-0"},
            },
            "hits_by_run": {"run:reset": 2},
            "disposition": {"kind": "eligible"},
        }
    ]


def _valid_outcomes() -> dict[str, object]:
    return {
        "rollups": [
            {
                "metric": "line",
                "semantics": "One Verilator basic-block point; covered when its count is greater than zero.",
                "total_points": 1,
                "eligible_points": 1,
                "covered_points": 1,
                "waived_points": 0,
                "percent": 100.0,
            }
        ],
        "collection": {
            "status": "complete",
            "merge": {"status": "equivalent", "artifact": "artifact:merged"},
            "diagnostics": [],
        },
        "findings": [],
        "evaluation": {
            "status": "not_requested",
            "criterion_fingerprint": None,
            "suite": {"status": "not_evaluated"},
            "thresholds": {},
            "metrics": [],
            "diagnostics": [],
        },
    }


def _valid_document() -> dict[str, object]:
    return {
        "$schema": "booley.coverage-campaign/v1",
        "campaign_id": "campaign:sim_counter:2026-09-01T10:30:00Z",
        **_valid_provenance(),
        "tests": _valid_tests(),
        "artifacts": _valid_artifacts(),
        "normalization": {"status": "complete", "unrecognized_records": []},
        "points": _valid_points(),
        **_valid_outcomes(),
    }


def test_known_literal_decodes_to_immutable_campaign_and_encodes_canonically() -> None:
    document = _valid_document()

    campaign = decode_coverage_campaign(
        copy.deepcopy(document),
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    assert campaign.campaign_id == "campaign:sim_counter:2026-09-01T10:30:00Z"
    assert campaign.target.identity == "acme:demo:counter:1.0#sim_counter"
    assert campaign.runs[0].simulation_verdict == "pass"
    assert campaign.points[0].hits_by_run["run:reset"] == 2
    assert campaign.rollups[0].covered_points == 1
    assert encode_coverage_campaign(campaign) == document


def test_evaluated_campaign_preserves_exact_metric_evidence() -> None:
    document = _valid_document()
    document["evaluation"] = {
        "status": "pass",
        "criterion_fingerprint": _fingerprint("c"),
        "approved_waiver_set_digest": _fingerprint("d"),
        "suite": {
            "status": "match",
            "required": ["reset"],
            "selected": ["reset"],
        },
        "thresholds": {"line": 100},
        "metrics": [
            {
                "metric": "line",
                "total_points": 1,
                "eligible_points": 1,
                "covered_points": 1,
                "waived_points": 0,
                "actual_numerator": 100,
                "actual_denominator": 1,
                "actual_percent": 100.0,
                "minimum_percent": 100,
                "verdict": "pass",
            }
        ],
        "diagnostics": [],
    }

    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )

    assert encode_coverage_campaign(campaign) == document


def test_gated_campaign_requires_approved_waiver_set_digest() -> None:
    document = _valid_document()
    document["evaluation"] = {
        "status": "blocked",
        "criterion_fingerprint": _fingerprint("c"),
        "suite": {"status": "match", "required": ["reset"], "selected": ["reset"]},
        "thresholds": {"line": 100},
        "metrics": [],
        "diagnostics": [
            {
                "code": "COV_EVAL_EMPTY_DENOMINATOR",
                "pointer": "/rollups",
                "message": "Configured metric has no eligible points: line.",
            }
        ],
    }

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity(document["target"]["identity"]),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_FIELD_REQUIRED", "/evaluation/approved_waiver_set_digest")
    ]


def test_evaluated_metrics_use_fixed_order_independent_of_threshold_key_order() -> None:
    document = _valid_document()
    document["collector"]["capabilities"].append({"record_class": "branch", "status": "reported"})
    document["points"].append(
        {
            "id": _BRANCH_POINT_ID,
            "identity": {
                "metric": "branch",
                "location": {
                    "source": "rtl/counter.sv",
                    "start": {"line": 12, "column": 3},
                    "end": {"line": 12, "column": 28},
                },
                "hierarchy": "TOP.counter",
                "subject": {"outcome": "true"},
                "collector": {
                    "record_type": "v_branch",
                    "native_key": "12:branch-true",
                },
            },
            "hits_by_run": {"run:reset": 1},
            "disposition": {"kind": "eligible"},
        }
    )
    document["rollups"].append(
        {
            "metric": "branch",
            "semantics": (
                "One branch outcome; each outcome is a separate point covered when count is "
                "greater than zero."
            ),
            "total_points": 1,
            "eligible_points": 1,
            "covered_points": 1,
            "waived_points": 0,
            "percent": 100.0,
        }
    )
    metric_evidence = [
        {
            "metric": metric,
            "total_points": 1,
            "eligible_points": 1,
            "covered_points": 1,
            "waived_points": 0,
            "actual_numerator": 100,
            "actual_denominator": 1,
            "actual_percent": 100.0,
            "minimum_percent": 90,
            "verdict": "pass",
        }
        for metric in ("line", "branch")
    ]
    document["evaluation"] = {
        "status": "pass",
        "criterion_fingerprint": _fingerprint("c"),
        "approved_waiver_set_digest": _fingerprint("d"),
        "suite": {"status": "match", "required": ["reset"], "selected": ["reset"]},
        "thresholds": {"branch": 90, "line": 90},
        "metrics": metric_evidence,
        "diagnostics": [],
    }

    campaign = decode_coverage_campaign(
        document,
        DurableTargetIdentity(document["target"]["identity"]),
    )

    assert [item["metric"] for item in campaign.evaluation["metrics"]] == ["line", "branch"]


def test_decoder_aggregates_semantic_findings_with_stable_json_pointers() -> None:
    document = _valid_document()
    document["$schema"] = "booley.coverage-campaign/v99"
    document["target"]["identity"] = "acme:demo:other:1.0#sim_other"
    document["tests"]["declared"] = ["reset", "reset"]
    document["tests"]["selected"] = ["missing"]
    document["artifacts"][0]["path"] = "../../outside.dat"
    document["points"][0]["id"] = "cp1:not-the-identity"
    document["points"][0]["hits_by_run"] = {"run:ghost": 0}
    document["rollups"][0]["covered_points"] = 0

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_SCHEMA_VERSION_UNSUPPORTED", "/$schema"),
        ("COV_TARGET_MISMATCH", "/target/identity"),
        ("COV_DECLARED_TEST_DUPLICATE", "/tests/declared/1"),
        ("COV_SELECTED_TEST_UNDECLARED", "/tests/selected/0"),
        ("COV_SELECTED_TEST_RUN_CARDINALITY", "/tests/selected/0"),
        ("COV_RUN_FOR_UNSELECTED_TEST", "/tests/runs/0"),
        ("COV_ARTIFACT_PATH_UNSAFE", "/artifacts/0/path"),
        ("COV_POINT_ID_MISMATCH", "/points/0/id"),
        ("COV_POINT_RUN_UNKNOWN", "/points/0/hits_by_run/run:ghost"),
        ("COV_POINT_HIT_NONPOSITIVE", "/points/0/hits_by_run/run:ghost"),
        ("COV_ROLLUP_MISMATCH", "/rollups"),
    ]


def test_decoder_rejects_duplicate_most_specific_point_identity() -> None:
    document = _valid_document()
    document["points"].append(copy.deepcopy(document["points"][0]))
    document["rollups"][0].update({"total_points": 2, "eligible_points": 2, "covered_points": 2})

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_POINT_ID_DUPLICATE", "/points/1/id")
    ]


def test_unknown_native_record_requires_a_reported_capability_fact() -> None:
    document = _valid_document()
    document["normalization"] = {
        "status": "complete_with_unknown_records",
        "unrecognized_records": [
            {
                "raw_artifact": "artifact:raw-reset",
                "record_ordinal": 27,
                "native_type": "mystery-27",
                "state": "unknown_retained",
            }
        ],
    }

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        (
            "COV_UNKNOWN_RECORD_CAPABILITY_MISSING",
            "/normalization/unrecognized_records/0/native_type",
        ),
        (
            "COV_UNKNOWN_RECORD_FINDING_MISSING",
            "/normalization/unrecognized_records/0",
        ),
    ]


def test_incompatible_native_format_cannot_expose_normalized_points() -> None:
    document = _valid_document()
    document["collector"]["native_format"]["compatibility"] = "incompatible"

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_INCOMPATIBLE_FORMAT_NORMALIZED", "/normalization/status"),
        ("COV_INCOMPATIBLE_FORMAT_POINTS_PRESENT", "/points"),
        ("COV_INCOMPATIBLE_FORMAT_COLLECTION", "/collection/status"),
        ("COV_INCOMPATIBLE_FORMAT_EVALUATION", "/evaluation/status"),
    ]


def test_unknown_native_record_is_retained_without_blocking_known_evidence() -> None:
    document = _valid_document()
    document["collector"]["capabilities"].append(
        {"record_class": "native:mystery-27", "status": "reported"}
    )
    document["normalization"] = {
        "status": "complete_with_unknown_records",
        "unrecognized_records": [
            {
                "raw_artifact": "artifact:raw-reset",
                "record_ordinal": 27,
                "native_type": "mystery-27",
                "state": "unknown_retained",
            }
        ],
    }
    document["findings"] = [
        {
            "severity": "info",
            "code": "COV_NATIVE_RECORD_UNKNOWN",
            "pointer": "/normalization/unrecognized_records/0",
            "message": "Native record class is retained but not normalized as a point.",
        }
    ]

    campaign = decode_coverage_campaign(
        document,
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    assert campaign.collector.capabilities[-1].status == "reported"
    assert campaign.normalization["unrecognized_records"][0]["record_ordinal"] == 27
    assert encode_coverage_campaign(campaign) == document


def test_unknown_native_record_requires_artifact_and_finding_evidence() -> None:
    document = _valid_document()
    document["collector"]["capabilities"].append(
        {"record_class": "native:mystery-27", "status": "reported"}
    )
    document["normalization"] = {
        "status": "complete_with_unknown_records",
        "unrecognized_records": [
            {
                "raw_artifact": "artifact:missing",
                "record_ordinal": 27,
                "native_type": "mystery-27",
                "state": "unknown_retained",
            }
        ],
    }

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        (
            "COV_UNKNOWN_RECORD_ARTIFACT_UNKNOWN",
            "/normalization/unrecognized_records/0/raw_artifact",
        ),
        (
            "COV_UNKNOWN_RECORD_FINDING_MISSING",
            "/normalization/unrecognized_records/0",
        ),
    ]


def test_incompatible_native_format_round_trips_without_normalized_evidence() -> None:
    document = _valid_document()
    document["collector"]["native_format"]["compatibility"] = "incompatible"
    document["normalization"] = {"status": "incompatible", "unrecognized_records": []}
    document["points"] = []
    document["rollups"] = []
    document["collection"] = {
        "status": "incompatible",
        "merge": {"status": "not_attempted", "artifact": None},
        "diagnostics": ["COV_NATIVE_FORMAT_INCOMPATIBLE"],
    }
    document["evaluation"]["status"] = "blocked"
    document["evaluation"]["diagnostics"] = ["COV_NATIVE_FORMAT_INCOMPATIBLE"]

    campaign = decode_coverage_campaign(
        document,
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    assert campaign.points == ()
    assert campaign.normalization["status"] == "incompatible"
    assert encode_coverage_campaign(campaign) == document


def test_incompatible_native_format_requires_blocked_collection_and_evaluation() -> None:
    document = _valid_document()
    document["collector"]["native_format"]["compatibility"] = "incompatible"
    document["normalization"] = {"status": "incompatible", "unrecognized_records": []}
    document["points"] = []
    document["rollups"] = []
    document["collection"] = {
        "status": "incomplete",
        "merge": {"status": "not_attempted", "artifact": None},
        "diagnostics": [],
    }
    document["evaluation"]["status"] = "not_requested"

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_INCOMPATIBLE_FORMAT_COLLECTION", "/collection/status"),
        ("COV_INCOMPATIBLE_FORMAT_EVALUATION", "/evaluation/status"),
    ]


def test_decoded_campaign_is_deeply_immutable() -> None:
    campaign = decode_coverage_campaign(
        _valid_document(),
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    with pytest.raises(FrozenInstanceError):
        campaign.campaign_id = "replacement"
    with pytest.raises(TypeError):
        campaign.points[0].hits_by_run["run:reset"] = 4
    with pytest.raises(TypeError):
        campaign.points[0].identity.subject["basic_block"] = 3


def test_decoder_aggregates_structural_findings_before_model_construction() -> None:
    document = _valid_document()
    del document["campaign_id"]
    document["invocation"] = []
    document["collector"]["capabilities"][0]["status"] = 7
    document["tests"]["runs"][0]["test"] = False
    document["artifacts"][0]["bytes"] = True
    document["points"][0]["identity"]["location"] = []

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_FIELD_REQUIRED", "/campaign_id"),
        ("COV_FIELD_TYPE", "/invocation"),
        ("COV_FIELD_TYPE", "/collector/capabilities/0/status"),
        ("COV_FIELD_TYPE", "/tests/runs/0/test"),
        ("COV_FIELD_TYPE", "/artifacts/0/bytes"),
        ("COV_FIELD_TYPE", "/points/0/identity/location"),
    ]


def test_decoder_aggregates_safe_semantic_findings_with_structural_findings() -> None:
    document = _valid_document()
    del document["campaign_id"]
    document["$schema"] = "booley.coverage-campaign/v99"
    document["target"]["identity"] = "acme:demo:other:1.0#sim_other"

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_FIELD_REQUIRED", "/campaign_id"),
        ("COV_SCHEMA_VERSION_UNSUPPORTED", "/$schema"),
        ("COV_TARGET_MISMATCH", "/target/identity"),
    ]


def test_decoder_rejects_unparseable_invocation_timestamp() -> None:
    document = _valid_document()
    document["invocation"]["started_at"] = "yesterday"

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_TIMESTAMP_INVALID", "/invocation/started_at")
    ]


def test_encoder_canonicalizes_legacy_invocation_timestamp() -> None:
    document = _valid_document()
    document["invocation"]["started_at"] = "2026-09-01T14:30:00.987654+04:00"

    campaign = decode_coverage_campaign(
        document,
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    encoded = encode_coverage_campaign(campaign)
    assert encoded["invocation"]["started_at"] == "2026-09-01T10:30:00Z"


def test_complete_collection_requires_fresh_raw_and_verified_merged_artifacts() -> None:
    document = _valid_document()
    document["artifacts"][0]["state"] = "stale"
    document["artifacts"][1]["state"] = "stale"
    document["collection"]["merge"]["status"] = "mismatch"

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_INCLUDED_RUN_RAW_INVALID", "/tests/runs/0/raw_artifact"),
        ("COV_COMPLETE_MERGE_UNVERIFIED", "/collection/merge/status"),
        ("COV_COMPLETE_MERGED_ARTIFACT_INVALID", "/collection/merge/artifact"),
    ]


def test_points_require_reported_capabilities_and_exact_dispositions() -> None:
    document = _valid_document()
    document["collector"]["capabilities"][0]["status"] = "maybe"
    document["points"][0]["disposition"] = {"kind": "waived"}
    document["rollups"][0].update(
        {
            "eligible_points": 0,
            "covered_points": 0,
            "waived_points": 1,
            "percent": None,
        }
    )

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_CAPABILITY_STATUS_INVALID", "/collector/capabilities/0/status"),
        ("COV_POINT_CAPABILITY_MISSING", "/points/0/identity/metric"),
        ("COV_WAIVER_REFERENCE_INCOMPLETE", "/points/0/disposition"),
    ]


def test_reported_only_point_must_be_unscored() -> None:
    document = _valid_document()
    point = document["points"][0]
    point["id"] = _FSM_POINT_ID
    point["identity"] = {
        "metric": "fsm",
        "location": {
            "source": "rtl/counter.sv",
            "start": {"line": 10, "column": 3},
            "end": {"line": 10, "column": 28},
        },
        "hierarchy": "TOP.counter",
        "subject": {"machine": "counter_state", "transition": "RUN_to_WRAP"},
        "collector": {"record_type": "v_fsm", "native_key": "10:fsm-run-wrap"},
    }
    point["disposition"] = {
        "kind": "waived",
        "waiver_id": "waiver:fsm",
        "waiver_file": "rtl/counter.sv.json",
        "waiver_fingerprint": _fingerprint("e"),
    }
    document["collector"]["capabilities"] = [{"record_class": "fsm", "status": "reported"}]
    document["rollups"] = []

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_REPORTED_ONLY_POINT_SCORED", "/points/0/disposition")
    ]


def test_decoder_validates_campaign_state_sections_before_semantic_checks() -> None:
    document = _valid_document()
    document["build"]["coverage"] = "yes"
    document["collection"].pop("status")
    document["collection"]["merge"] = []
    document["evaluation"]["status"] = 1

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_FIELD_TYPE", "/build/coverage"),
        ("COV_FIELD_REQUIRED", "/collection/status"),
        ("COV_FIELD_TYPE", "/collection/merge"),
        ("COV_FIELD_TYPE", "/evaluation/status"),
    ]


def test_decoder_recomputes_stored_evaluation_from_rollups_and_thresholds() -> None:
    document = _valid_document()
    document["evaluation"] = {
        "status": "fail",
        "criterion_fingerprint": _fingerprint("d"),
        "approved_waiver_set_digest": _fingerprint("e"),
        "suite": {"status": "complete"},
        "thresholds": {"line": 50.0},
        "metrics": [
            {
                "metric": "line",
                "actual_percent": 100.0,
                "minimum_percent": 50.0,
                "verdict": "fail",
            }
        ],
        "diagnostics": [],
    }

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_EVALUATION_MISMATCH", "/evaluation")
    ]


def test_decoder_rejects_nonfinite_threshold_input() -> None:
    document = _valid_document()
    document["evaluation"]["thresholds"] = {"line": float("nan")}

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_FIELD_TYPE", "/evaluation/thresholds/line")
    ]


def test_only_rtl_source_closure_points_can_be_scored() -> None:
    document = _valid_document()
    document["points"][0]["identity"]["location"]["source"] = "tb/counter_tb.sv"
    document["points"][0]["id"] = _TESTBENCH_POINT_ID

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_SCORED_POINT_OUTSIDE_RTL", "/points/0/disposition")
    ]


def test_encoder_canonicalizes_nested_json_object_key_order() -> None:
    first = _valid_document()
    second = copy.deepcopy(first)
    source = second["source_closure"]["rtl"][0]
    second["source_closure"]["rtl"][0] = {
        "sha256": source["sha256"],
        "path": source["path"],
    }

    first_campaign = decode_coverage_campaign(
        first,
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )
    second_campaign = decode_coverage_campaign(
        second,
        DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
    )

    assert json.dumps(encode_coverage_campaign(first_campaign)) == json.dumps(
        encode_coverage_campaign(second_campaign)
    )


def test_decoder_rejects_duplicate_run_artifact_and_capability_identities() -> None:
    document = _valid_document()
    document["tests"]["runs"].append(copy.deepcopy(document["tests"]["runs"][0]))
    document["artifacts"].append(copy.deepcopy(document["artifacts"][0]))
    document["collector"]["capabilities"].append(
        copy.deepcopy(document["collector"]["capabilities"][0])
    )

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_RUN_ID_DUPLICATE", "/tests/runs/1/id"),
        ("COV_SELECTED_TEST_RUN_CARDINALITY", "/tests/selected/0"),
        ("COV_ARTIFACT_ID_DUPLICATE", "/artifacts/2/id"),
        ("COV_CAPABILITY_DUPLICATE", "/collector/capabilities/1/record_class"),
    ]


def test_decoder_rejects_unknown_run_states() -> None:
    document = _valid_document()
    document["tests"]["runs"][0]["simulation_verdict"] = "maybe"
    document["tests"]["runs"][0]["collection"] = "sometimes"
    document["collection"] = {
        "status": "incomplete",
        "merge": {"status": "not_attempted", "artifact": None},
        "diagnostics": [],
    }

    with pytest.raises(CoverageCampaignValidationError) as caught:
        decode_coverage_campaign(
            document,
            DurableTargetIdentity("acme:demo:counter:1.0#sim_counter"),
        )

    assert [(finding.code, finding.pointer) for finding in caught.value.findings] == [
        ("COV_SIMULATION_VERDICT_INVALID", "/tests/runs/0/simulation_verdict"),
        ("COV_RUN_COLLECTION_STATE_INVALID", "/tests/runs/0/collection"),
    ]
