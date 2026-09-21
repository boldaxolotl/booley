from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booley.flows.sim.campaign.codec import (
    CampaignIntegrityError,
    canonical_json_bytes,
    decode_campaign_manifest,
    encode_campaign_manifest,
)
from booley.flows.sim.campaign.model import SimulationCampaignManifest


def _sha(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _manifest() -> dict[str, object]:
    target = {
        "vlnv": "acme:lib:dut:1",
        "name": "sim",
        "selector": "sim",
        "project_identity": "project",
        "revision": "abc123",
        "role": "candidate",
        "display_name": "sim",
    }
    source_recipe = {
        "sources": [],
        "parameters": [],
        "defines": [],
        "pre_sim_commands": [],
    }
    build_recipe = {
        "backend": "icarus",
        "toplevel": "tb",
        "arguments": [],
        "command_model_sha256": "sha256:" + "a" * 64,
    }
    workload = {
        "mode": "simulate",
        "trace": False,
        "coverage": False,
        "eda": {"kind": "icarus", "version": "12"},
        "planner_contract_version": "1",
        "adapter_contract_version": "1",
        "pre_sim_build_access": "immutable",
        "run_cwd": {"configured": "run", "kind": "literal", "placeholders": []},
        "runtime_inputs": [],
        "source_recipe": source_recipe,
        "build_recipe": build_recipe,
    }
    suite = {
        "names": [],
        "default_invocation": True,
        "source_path": "",
        "source_bytes": 0,
        "source_sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
    }
    variant_recipe = {
        "kind": "candidate",
        "source_closure": [],
        "source_recipe": source_recipe,
        "build_recipe": build_recipe,
        "eda": workload["eda"],
        "trace": False,
        "coverage": False,
    }
    variant_sha = _sha(variant_recipe)
    variants = [
        {
            "build_variant_id": "variant:" + variant_sha.removeprefix("sha256:"),
            "kind": "candidate",
            "sharing_eligible": True,
            "source_closure": [],
            "recipe_sha256": variant_sha,
        }
    ]
    item_identity = {
        "ordinal": 0,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": target,
        "selection": {"kind": "default", "names": []},
        "arguments": [],
        "build_variant_id": variants[0]["build_variant_id"],
        "run_directory": {"configured": "run", "kind": "literal", "collision_template": "run"},
    }
    item_sha = _sha(item_identity)
    items = [
        {
            "work_item_id": "item:0000:" + item_sha.removeprefix("sha256:")[:16],
            **item_identity,
            "fingerprint_sha256": item_sha,
        }
    ]
    manifest = {
        "$schema": "booley.simulation-campaign-manifest/v1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "created_at": "2026-09-21T10:00:00Z",
        "origin": {"execution_id": "", "invocation_id": 1},
        "target": target,
        "workload": workload,
        "required_suite": suite,
        "build_variants": variants,
        "planning_disclosures": [],
        "prerequisites": [],
        "work_items": items,
        "fingerprints": {},
    }
    manifest["fingerprints"] = {
        "target_recipe_sha256": _sha(
            {"target": target, "source_recipe": source_recipe, "build_recipe": build_recipe}
        ),
        "source_closures_sha256": _sha(
            [{"build_variant_id": variants[0]["build_variant_id"], "source_closure": []}]
        ),
        "required_suite_sha256": _sha(suite),
        "planning_disclosures_sha256": _sha([]),
        "prerequisites_sha256": _sha([]),
        "work_items_sha256": _sha(items),
        "workload_sha256": _sha(
            {
                key: manifest[key]
                for key in (
                    "target",
                    "workload",
                    "required_suite",
                    "build_variants",
                    "planning_disclosures",
                    "prerequisites",
                    "work_items",
                )
            }
        ),
    }
    return manifest


def test_manifest_exact_codec_recomputes_all_component_digests() -> None:
    raw = canonical_json_bytes(_manifest())
    value = decode_campaign_manifest(raw)
    assert value.canonical_bytes() == raw
    assert encode_campaign_manifest(value) == raw


@given(st.sampled_from(["workload_sha256", "target_recipe_sha256", "work_items_sha256"]))
def test_manifest_digest_mutations_are_rejected(field: str) -> None:
    manifest = _manifest()
    manifest["fingerprints"][field] = "sha256:" + "f" * 64  # type: ignore[index]
    with pytest.raises(CampaignIntegrityError, match="disagrees"):
        decode_campaign_manifest(canonical_json_bytes(manifest))


@given(st.sampled_from(["missing", "unknown", "type", "path", "digest", "conditional"]))
def test_manifest_structural_mutations_are_rejected(mutation: str) -> None:
    manifest = _manifest()
    target = manifest["target"]
    if mutation == "missing":
        del target["revision"]
    elif mutation == "unknown":
        target["unexpected"] = True
    elif mutation == "type":
        target["name"] = 7
    elif mutation == "path":
        manifest["required_suite"]["source_path"] = "../escape"  # type: ignore[index]
    elif mutation == "digest":
        manifest["fingerprints"]["workload_sha256"] = "sha256:" + "f" * 64  # type: ignore[index]
    else:
        manifest["work_items"][0]["role"] = "cycle_count_baseline"  # type: ignore[index]
    with pytest.raises(CampaignIntegrityError):
        decode_campaign_manifest(canonical_json_bytes(manifest))
    with pytest.raises(CampaignIntegrityError):
        encode_campaign_manifest(SimulationCampaignManifest(manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defines", ["x" * 4097]),
        ("pre_sim_commands", ["x" * (16 * 1024 + 1)]),
    ],
)
def test_manifest_rejects_recipe_string_resource_overflow(field: str, value: object) -> None:
    manifest = _manifest()
    manifest["workload"]["source_recipe"][field] = value  # type: ignore[index]
    with pytest.raises(CampaignIntegrityError, match="ceiling"):
        decode_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "cycle_count_baseline"), ("revision", "different")],
)
def test_work_item_role_and_revision_must_match_target(field: str, value: str) -> None:
    manifest = _manifest()
    manifest["work_items"][0][field] = value  # type: ignore[index]
    with pytest.raises(CampaignIntegrityError, match="role/revision"):
        decode_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize("names", [[], [1], ["smoke", "smoke"]])
def test_named_selection_requires_unique_nonempty_bounded_strings(names: list[object]) -> None:
    manifest = _manifest()
    manifest["work_items"][0]["selection"] = {"kind": "named", "names": names}  # type: ignore[index]
    with pytest.raises(CampaignIntegrityError):
        decode_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize("mutation", ["target_role", "work_item_id"])
def test_prerequisite_binds_a_baseline_target_and_valid_work_item(mutation: str) -> None:
    manifest = _manifest()
    target = dict(manifest["target"])  # type: ignore[arg-type]
    target["role"] = "candidate" if mutation == "target_role" else "cycle_count_baseline"
    prerequisite = {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": "targets/baseline/manifest.json",
            "bytes": 1,
            "sha256": "sha256:" + "a" * 64,
            "kind": "simulation_campaign_manifest",
            "owner": "550e8400-e29b-41d4-a716-446655440000",
        },
        "campaign_id": "550e8400-e29b-41d4-a716-446655440000",
        "target": target,
        "required_observation": "cycle_count",
        "work_item_id": (
            "not-an-item" if mutation == "work_item_id" else "item:0000:0123456789abcdef"
        ),
    }
    manifest["prerequisites"] = [prerequisite]
    with pytest.raises(CampaignIntegrityError):
        decode_campaign_manifest(canonical_json_bytes(manifest))
