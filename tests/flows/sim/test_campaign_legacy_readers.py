"""Read-only compatibility for campaigns predating recorded Test Runs."""

import pytest

from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.planning import finalize_manifest
from tests.flows.sim.campaign_legacy_fixtures import _legacy_manifest
from tests.flows.sim.test_campaign_manifest_codec import (
    _sha,
)
from tests.flows.sim.test_campaign_test_runs import (
    _manifest_run_groups,
    _work_item_test_runs,
)

_V1 = "booley.simulation-campaign-manifest/v1"
_V2 = "booley.simulation-campaign-manifest/v2"


def _refinalized(schema: str, **workload_members: object):
    """Re-finalize the fixture so fingerprints stay valid and only the workload rule varies."""
    document = _legacy_manifest(schema.rsplit("/", 1)[-1])
    decode_simulation_campaign_manifest(canonical_json_bytes(document))
    document.pop("fingerprints")
    document["$schema"] = schema
    workload = document["workload"]
    workload.pop("no_waivers", None)
    workload.update(workload_members)  # type: ignore[union-attr]
    # The build variant recipe binds ``workload.coverage``; keep it consistent.
    variant = document["build_variants"][0]  # type: ignore[index]
    variant["source_closure"] = []
    recipe_sha = _sha(
        {
            "kind": variant["kind"],
            "source_closure": variant["source_closure"],
            "source_recipe": workload["source_recipe"],  # type: ignore[index]
            "build_recipe": workload["build_recipe"],  # type: ignore[index]
            "eda": workload["eda"],  # type: ignore[index]
            "trace": workload["trace"],  # type: ignore[index]
            "coverage": workload["coverage"],  # type: ignore[index]
        }
    )
    variant["recipe_sha256"] = recipe_sha
    variant["build_variant_id"] = "variant:" + recipe_sha.removeprefix("sha256:")
    item = document["work_items"][0]  # type: ignore[index]
    item["build_variant_id"] = variant["build_variant_id"]
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    item["fingerprint_sha256"] = _sha(identity)
    item["work_item_id"] = "item:0000:" + item["fingerprint_sha256"][7:23]
    return finalize_manifest(document)


def test_no_waivers_v2_manifest_decodes_and_keeps_its_schema() -> None:
    manifest = _refinalized(_V2, coverage=True, no_waivers=True)
    assert manifest.document["$schema"] == _V2
    assert manifest.document["workload"]["no_waivers"] is True  # type: ignore[index]
    decoded = decode_simulation_campaign_manifest(encode_simulation_campaign_manifest(manifest))
    assert decoded.document == manifest.document


def test_v1_manifest_grammar_is_unchanged_by_no_waivers_support() -> None:
    manifest = _refinalized(_V1, coverage=True)
    assert manifest.document["$schema"] == _V1
    assert "no_waivers" not in manifest.document["workload"]  # type: ignore[operator]


def test_a_manifest_before_v4_still_decodes_for_reports():
    manifest = finalize_manifest(
        {k: v for k, v in _legacy_manifest().items() if k != "fingerprints"}
    )
    decoded = decode_simulation_campaign_manifest(encode_simulation_campaign_manifest(manifest))
    assert decoded.document["$schema"] == "booley.simulation-campaign-manifest/v1"
    assert all("test_runs" not in item for item in decoded.document["work_items"])
    assert _work_item_test_runs(decoded.document["work_items"][0]) is None


def test_resuming_a_manifest_before_v4_fails_closed():
    manifest = finalize_manifest(
        {k: v for k, v in _legacy_manifest().items() if k != "fingerprints"}
    )
    with pytest.raises(SimulationCampaignIntegrityError) as excinfo:
        _manifest_run_groups(manifest)
    assert str(excinfo.value) == (
        "this Simulation Campaign manifest predates recorded Test Runs "
        "(booley.simulation-campaign-manifest/v1) and cannot be resumed; "
        "start a new Simulation Campaign"
    )
