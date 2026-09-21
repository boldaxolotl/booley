"""Independent canonical bytes for every Phase-1 terminal discriminator."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booley.flows.sim.campaign.codec import (
    CampaignIntegrityError,
    canonical_json_bytes,
    decode_build_result,
    decode_simulation_result,
    encode_bundle_build_result,
    encode_simulation_result,
)
from booley.flows.sim.campaign.model import BundleBuildResult, SimulationResult

_BUILD_READY = b'{"$schema":"booley.bundle-build-result/v1","build_attempt":{"build_attempt_id":"550e8400-e29b-41d4-a716-446655440000","bytes":1,"kind":"bundle_build_attempt","owner":"550e8400-e29b-41d4-a716-446655440000","path":"build-attempt.json","sha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"build_variant_id":"variant:3333333333333333333333333333333333333333333333333333333333333333","bundle":{"artifacts":[{"bytes":1,"kind":"simulator_executable","path":"simv","sha256":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],"bundle_id":"6ba7b810-9dad-41d1-80b4-00c04fd430c8","manifest_bytes":1,"manifest_path":"evidence/bundle.json","manifest_sha256":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","sharing":"shared_variant"},"campaign_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","elapsed_seconds":1.0,"evidence":[],"finished_at":"2026-09-21T10:00:01Z","manifest_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111","observation":null,"phase":"ready","state":"ready","workload_sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}\n'
_BUILD_DESIGN = b'{"$schema":"booley.bundle-build-result/v1","build_attempt":{"build_attempt_id":"550e8400-e29b-41d4-a716-446655440000","bytes":1,"kind":"bundle_build_attempt","owner":"550e8400-e29b-41d4-a716-446655440000","path":"build-attempt.json","sha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"build_variant_id":"variant:3333333333333333333333333333333333333333333333333333333333333333","bundle":null,"campaign_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","elapsed_seconds":1.0,"evidence":[],"finished_at":"2026-09-21T10:00:01Z","manifest_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111","observation":{"class":"design","code":"compile","detail":{},"message":"compile failed"},"phase":"compile","state":"design_failure","workload_sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}\n'
_BUILD_INFRA = b'{"$schema":"booley.bundle-build-result/v1","build_attempt":{"build_attempt_id":"550e8400-e29b-41d4-a716-446655440000","bytes":1,"kind":"bundle_build_attempt","owner":"550e8400-e29b-41d4-a716-446655440000","path":"build-attempt.json","sha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"build_variant_id":"variant:3333333333333333333333333333333333333333333333333333333333333333","bundle":null,"campaign_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","elapsed_seconds":1.0,"evidence":[],"finished_at":"2026-09-21T10:00:01Z","manifest_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111","observation":{"class":"infrastructure","code":"storage","detail":{},"message":"storage failed"},"phase":"storage","state":"infrastructure_error","workload_sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}\n'


@pytest.mark.parametrize("raw", [_BUILD_READY, _BUILD_DESIGN, _BUILD_INFRA])
def test_build_result_golden_discriminators(raw: bytes) -> None:
    value = decode_build_result(raw)
    assert value.canonical_bytes() == raw
    assert encode_bundle_build_result(value) == raw


def _simulation_result(state: str) -> bytes:
    blocked = state == "blocked_by_build"
    execution = (
        state if state in {"timeout", "crash", "setup_error", "blocked_by_build"} else "completed"
    )
    build_state = "design_failure" if blocked else "ready"
    bundle_id = "null" if blocked else '"6ba7b810-9dad-41d1-80b4-00c04fd430c8"'
    snapshot = (
        "null"
        if state in {"blocked_by_build", "setup_error"}
        else '{"bundle_manifest_sha256":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","manifest":{"bytes":1,"kind":"executable_snapshot_manifest","owner":"550e8400-e29b-41d4-a716-446655440001","path":"evidence/snapshot.json","sha256":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},"post_exit_sha256":"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","pre_launch_sha256":"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","verified_after_exit":true}'
    )
    failure = '"design"' if state != "completed" else "null"
    functional = '"pass"' if state == "completed" else '"not_observed"'
    assertions = '"clean"' if state == "completed" else '"not_observed"'
    grade = "pass" if state == "completed" else "fail"
    return (
        '{"$schema":"booley.simulation-result/v1","attempt_id":"550e8400-e29b-41d4-a716-446655440001","attempt_ordinal":1,'
        f'"build_result":{{"build_attempt_id":"550e8400-e29b-41d4-a716-446655440000","bytes":1,"kind":"bundle_build_result","owner":"550e8400-e29b-41d4-a716-446655440000","path":"build-result.json","sha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sharing":"shared_variant","state":"{build_state}"}},'
        f'"bundle_id":{bundle_id},"campaign_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","diagnostics":[],"elapsed_seconds":1.0,"evidence":[],"executable_snapshot":{snapshot},"finished_at":"2026-09-21T10:00:02Z","grade":"{grade}","manifest_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
        f'"observations":[{{"assertion_count":0,"assertions":{assertions},"cycle_count":null,"detail":{{}},"execution":"{execution}","failure_class":{failure},"functional":{functional},"test":"smoke"}}],"producer_invocation_id":1,"runtime_inputs":[],"state":"{state}","work_item_id":"item:0000:0123456789abcdef","workload_sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}}\n'
    ).encode()


@pytest.mark.parametrize(
    "state", ["completed", "timeout", "crash", "setup_error", "blocked_by_build"]
)
def test_simulation_result_golden_discriminators(state: str) -> None:
    raw = _simulation_result(state)
    value = decode_simulation_result(raw)
    assert value.canonical_bytes() == raw
    assert encode_simulation_result(value) == raw


@given(
    st.sampled_from(["missing", "unknown", "type", "path", "digest", "conditional"]),
    st.sampled_from(["build", "simulation"]),
)
def test_result_structural_mutations_are_rejected(mutation: str, kind: str) -> None:
    raw = _BUILD_READY if kind == "build" else _simulation_result("completed")
    document = json.loads(raw)
    decoder = decode_build_result if kind == "build" else decode_simulation_result
    reference = document["build_attempt" if kind == "build" else "build_result"]
    if mutation == "missing":
        del reference["sha256"]
    elif mutation == "unknown":
        document["evidence"] = [
            {
                "path": "evidence/log.txt",
                "bytes": 0,
                "sha256": "sha256:" + "0" * 64,
                "kind": "log",
                "owner": reference["owner"],
                "unexpected": True,
            }
        ]
    elif mutation == "type":
        document["elapsed_seconds"] = "1"
    elif mutation == "path":
        reference["path"] = "../escape"
    elif mutation == "digest":
        document["manifest_sha256"] = "sha256:bad"
    elif kind == "build":
        document["bundle"] = None
    else:
        document["executable_snapshot"] = None
    with pytest.raises(CampaignIntegrityError):
        decoder(canonical_json_bytes(document))
    value_type = BundleBuildResult if kind == "build" else SimulationResult
    encoder = encode_bundle_build_result if kind == "build" else encode_simulation_result
    with pytest.raises(CampaignIntegrityError):
        encoder(value_type(document))


@pytest.mark.parametrize("field", ["test", "detail"])
def test_simulation_result_rejects_observation_resource_overflow(field: str) -> None:
    document = json.loads(_simulation_result("completed"))
    document["observations"][0][field] = "x" * (513 if field == "test" else 2049)
    with pytest.raises(CampaignIntegrityError, match="ceiling"):
        decode_simulation_result(canonical_json_bytes(document))


def test_nested_evidence_reference_rejects_unknown_fields() -> None:
    document = json.loads(_simulation_result("completed"))
    document["executable_snapshot"]["manifest"]["unexpected"] = True
    with pytest.raises(CampaignIntegrityError, match="exact fields"):
        decode_simulation_result(canonical_json_bytes(document))
