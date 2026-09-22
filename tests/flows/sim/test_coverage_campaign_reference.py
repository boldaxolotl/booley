"""Hostile public-seam tests for Simulation-owned Coverage Campaign references."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from booley.flows.sim import coverage_reference
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from booley.flows.sim.campaign.facts import AcceptanceFacts
from booley.flows.sim.coverage_campaign import (
    DurableTargetIdentity,
    decode_coverage_campaign,
)
from booley.flows.sim.coverage_campaign_store import publish_coverage_campaign
from booley.flows.sim.coverage_reference import (
    MAX_REFERENCE_BYTES,
    CoverageCampaignReference,
    CoverageCampaignReferenceError,
    build_coverage_campaign_reference,
    decode_coverage_campaign_reference,
    encode_coverage_campaign_reference,
    publish_coverage_campaign_reference,
    resolve_coverage_campaign_reference,
)
from tests.flows.sim.test_coverage_campaign import _valid_document

_SIMULATION_CAMPAIGN_ID = "12345678-1234-4234-9234-123456789abc"
_SIMULATION_ATTEMPT_ID = "87654321-4321-4432-a234-cba987654321"
_WORK_ITEM_ID = "item:0000:0123456789abcdef"
_TARGET_IDENTITY = "acme:demo:counter:1.0#sim_counter"
_TARGET_SELECTOR = "sim_counter"
_MANIFEST_SHA256 = "sha256:" + "a" * 64
_ORIGIN_INVOCATION_ID = 12


def _nested_campaign(origin_target: Path, *, attempt_id: str = _SIMULATION_ATTEMPT_ID) -> Path:
    attempt = (
        origin_target
        / "campaign"
        / "work-items"
        / "0001-0123456789abcdef"
        / "attempts"
        / f"0001-{attempt_id}"
    )
    campaign = decode_coverage_campaign(
        _valid_document(), DurableTargetIdentity(_TARGET_IDENTITY)
    )
    return publish_coverage_campaign(attempt / "coverage-campaign", campaign).campaign


def _reference(origin_target: Path, nested: Path) -> CoverageCampaignReference:
    return build_coverage_campaign_reference(
        simulation_campaign_id=_SIMULATION_CAMPAIGN_ID,
        simulation_manifest_sha256=_MANIFEST_SHA256,
        target_identity=_TARGET_IDENTITY,
        target_selector=_TARGET_SELECTOR,
        origin_invocation_id=_ORIGIN_INVOCATION_ID,
        producer_invocation_id=19,
        simulation_work_item_id=_WORK_ITEM_ID,
        simulation_attempt_id=_SIMULATION_ATTEMPT_ID,
        origin_target_directory=origin_target,
        coverage_campaign_path=nested,
    )


def _rewrite_reference(
    value: CoverageCampaignReference, pointer: tuple[str, ...], replacement: object
) -> bytes:
    document = json.loads(encode_coverage_campaign_reference(value))
    selected: dict[str, object] = document
    for component in pointer[:-1]:
        selected = selected[component]  # type: ignore[assignment]
    selected[pointer[-1]] = replacement
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )


def _facts_with_reference(value: CoverageCampaignReference) -> dict[str, object]:
    raw = encode_coverage_campaign_reference(value)
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": _SIMULATION_CAMPAIGN_ID,
        "manifest_sha256": _MANIFEST_SHA256,
        "origin": {"execution_id": "", "invocation_id": _ORIGIN_INVOCATION_ID},
        "target": {
            "vlnv": "acme:demo:counter:1.0", "name": _TARGET_SELECTOR,
            "selector": _TARGET_SELECTOR, "project_identity": "project",
            "revision": "abc", "role": "candidate", "display_name": _TARGET_SELECTOR,
        },
        "required_suite": {
            "names": ["reset"], "default_invocation": False,
            "source_sha256": "sha256:" + "b" * 64,
        },
        "prerequisites": [], "consumed_results": [], "observations": [],
        "coverage_reference": {
            "reference": {
                "path_base": "origin_invocation",
                "path": f"targets/{_TARGET_SELECTOR}/coverage.json",
                "bytes": len(raw), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "kind": "coverage_campaign_reference", "owner": _SIMULATION_CAMPAIGN_ID,
            },
            "document": json.loads(raw),
        },
    }


def test_acceptance_facts_reject_public_coverage_reference_substitution(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    facts = _facts_with_reference(_reference(target, _nested_campaign(target)))
    coverage = facts["coverage_reference"]
    assert isinstance(coverage, dict)
    document = coverage["document"]
    assert isinstance(document, dict)
    document["simulation_manifest_sha256"] = "sha256:" + "c" * 64
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    evidence = coverage["reference"]
    assert isinstance(evidence, dict)
    evidence["bytes"] = len(raw)
    evidence["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()

    with pytest.raises(SimulationCampaignIntegrityError, match="disagrees"):
        AcceptanceFacts(facts)


def test_acceptance_facts_reject_enclosing_reference_owner_substitution(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    facts = _facts_with_reference(_reference(target, _nested_campaign(target)))
    coverage = facts["coverage_reference"]
    assert isinstance(coverage, dict)
    reference = coverage["reference"]
    assert isinstance(reference, dict)
    reference["owner"] = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"

    with pytest.raises(SimulationCampaignIntegrityError, match="disagrees"):
        AcceptanceFacts(facts)


def test_reference_codec_round_trips_exact_canonical_immutable_document(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    value = _reference(target, nested)

    raw = encode_coverage_campaign_reference(value)
    decoded = decode_coverage_campaign_reference(raw)

    assert encode_coverage_campaign_reference(decoded) == raw
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    with pytest.raises(TypeError):
        decoded.document["origin_invocation_id"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        decoded.document["target"]["identity"] = "substitute"  # type: ignore[index]


@pytest.mark.parametrize(
    ("pointer", "replacement"),
    [
        (("$schema",), "booley.coverage-campaign-reference/v2"),
        (("simulation_campaign_id",), "12345678-1234-1234-9234-123456789abc"),
        (("simulation_manifest_sha256",), "sha256:" + "A" * 64),
        (("origin_invocation_id",), True),
        (("origin_invocation_id",), 0),
        (("producer_invocation_id",), False),
        (("simulation_work_item_id",), "item:1:0123456789abcdef"),
        (("simulation_attempt_id",), "87654321-4321-1432-a234-cba987654321"),
        (("target", "identity"), ""),
        (("coverage_campaign", "path_base"), "report_root"),
        (("coverage_campaign", "path"), "/campaign/coverage.json"),
        (("coverage_campaign", "path"), "campaign/../coverage.json"),
        (("coverage_campaign", "path"), "campaign/work-items/\0/coverage.json"),
        (("coverage_campaign", "path"), "elsewhere/coverage.json"),
        (("coverage_campaign", "bytes"), True),
        (("coverage_campaign", "bytes"), 0),
        (("coverage_campaign", "sha256"), "sha256:" + "0" * 63),
        (("coverage_campaign", "campaign_id"), ""),
        (("coverage_campaign", "schema"), "booley.coverage-campaign/v2"),
    ],
)
def test_reference_codec_rejects_boundary_and_identity_substitutions(
    tmp_path: Path, pointer: tuple[str, ...], replacement: object
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    value = _reference(target, _nested_campaign(target))

    with pytest.raises(CoverageCampaignReferenceError):
        decode_coverage_campaign_reference(_rewrite_reference(value, pointer, replacement))


def test_reference_codec_rejects_unknown_fields_noncanonical_bytes_and_size() -> None:
    with pytest.raises(CoverageCampaignReferenceError):
        decode_coverage_campaign_reference(b"{}\n")
    with pytest.raises(CoverageCampaignReferenceError):
        decode_coverage_campaign_reference(b"{ }\n")
    with pytest.raises(CoverageCampaignReferenceError):
        decode_coverage_campaign_reference(b"x" * (MAX_REFERENCE_BYTES + 1))


@pytest.mark.parametrize("container", [(), ("target",), ("coverage_campaign",)])
def test_reference_codec_rejects_unknown_fields(
    tmp_path: Path, container: tuple[str, ...]
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    value = _reference(target, _nested_campaign(target))
    document = json.loads(encode_coverage_campaign_reference(value))
    selected = document if not container else document[container[0]]
    selected["substitute"] = "unexpected"  # type: ignore[index]
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"

    with pytest.raises(CoverageCampaignReferenceError, match="exact fields"):
        decode_coverage_campaign_reference(raw)


def test_publication_is_create_only_and_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    value = _reference(target, _nested_campaign(target))
    path = target / "coverage.json"

    assert publish_coverage_campaign_reference(path, value) == value
    recovered = publish_coverage_campaign_reference(path, value)
    assert encode_coverage_campaign_reference(recovered) == path.read_bytes()

    conflicting = decode_coverage_campaign_reference(
        _rewrite_reference(value, ("producer_invocation_id",), 20)
    )
    with pytest.raises(CoverageCampaignReferenceError, match="conflicts"):
        publish_coverage_campaign_reference(path, conflicting)


def test_resolver_authenticates_real_nested_campaign_at_exact_attempt_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    path = target / "coverage.json"
    publish_coverage_campaign_reference(path, _reference(target, nested))

    resolved = resolve_coverage_campaign_reference(path)

    assert resolved.campaign_path == nested
    assert resolved.loaded.campaign.campaign_id == _valid_document()["campaign_id"]
    assert resolved.loaded.campaign.target.identity == _TARGET_IDENTITY


def test_resolver_decodes_authenticated_reference_bytes_during_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    value = _reference(target, nested)
    honest = encode_coverage_campaign_reference(value)
    substitute = _rewrite_reference(value, ("producer_invocation_id",), 20)
    path = target / "coverage.json"
    path.write_bytes(honest)
    original = coverage_reference.decode_coverage_campaign_reference

    def swap_after_authenticated_read(raw: bytes) -> CoverageCampaignReference:
        path.write_bytes(substitute)
        return original(raw)

    monkeypatch.setattr(
        coverage_reference, "decode_coverage_campaign_reference", swap_after_authenticated_read
    )
    resolved = coverage_reference.resolve_coverage_campaign_reference(path)

    assert resolved.reference.document["producer_invocation_id"] == 19
    assert path.read_bytes() == substitute


def test_resolver_rejects_nested_byte_digest_substitution(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    path = target / "coverage.json"
    publish_coverage_campaign_reference(path, _reference(target, nested))
    nested.write_bytes(nested.read_bytes() + b" ")

    with pytest.raises(CoverageCampaignReferenceError, match="bytes disagree"):
        resolve_coverage_campaign_reference(path)


def test_resolver_rejects_reference_digest_substitution_even_for_valid_nested_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    value = _reference(target, nested)
    raw = _rewrite_reference(
        value,
        ("coverage_campaign", "sha256"),
        "sha256:" + hashlib.sha256(b"substitute").hexdigest(),
    )
    path = target / "coverage.json"
    path.write_bytes(raw)

    with pytest.raises(CoverageCampaignReferenceError, match="bytes disagree"):
        resolve_coverage_campaign_reference(path)


def test_resolver_rejects_linked_reference_and_nested_path_components(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_campaign(target)
    value = _reference(target, nested)
    real_reference = target / "real-reference.json"
    real_reference.write_bytes(encode_coverage_campaign_reference(value))
    (target / "coverage.json").symlink_to(real_reference.name)
    with pytest.raises(CoverageCampaignReferenceError, match="not a regular file"):
        resolve_coverage_campaign_reference(target / "coverage.json")

    (target / "coverage.json").unlink()
    actual_work_items = target / "campaign" / "work-items"
    moved_work_items = target / "campaign" / "real-work-items"
    actual_work_items.rename(moved_work_items)
    actual_work_items.symlink_to(moved_work_items.name)
    (target / "coverage.json").write_bytes(encode_coverage_campaign_reference(value))
    with pytest.raises(CoverageCampaignReferenceError, match="contains a link"):
        resolve_coverage_campaign_reference(target / "coverage.json")


def test_resolver_rejects_attempt_identity_as_unrelated_path_substring(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    honest = _nested_campaign(target)
    misleading = (
        target
        / "campaign"
        / "work-items"
        / "prefix-0123456789abcdef-suffix"
        / "attempts"
        / f"0001-prefix-{_SIMULATION_ATTEMPT_ID}-suffix"
        / "coverage-campaign"
        / "coverage.json"
    )
    misleading.parent.mkdir(parents=True)
    misleading.write_bytes(honest.read_bytes())
    (misleading.parent / "coverage-points.jsonl.gz").write_bytes(
        (honest.parent / "coverage-points.jsonl.gz").read_bytes()
    )
    value = _reference(target, misleading)
    path = target / "coverage.json"
    path.write_bytes(encode_coverage_campaign_reference(value))

    with pytest.raises(CoverageCampaignReferenceError, match="Attempt identity"):
        resolve_coverage_campaign_reference(path)
