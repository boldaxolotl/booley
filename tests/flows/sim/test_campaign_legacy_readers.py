"""Read-only compatibility for campaigns predating recorded Test Runs."""

from pathlib import Path

import pytest

from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.test_runs import build_test_run
from tests.flows.sim.campaign_legacy_fixtures import _legacy_manifest
from tests.flows.sim.test_campaign_manifest_codec import (
    _names_only_work_items,
    _prepared_disclosure,
    _sha,
)
from tests.flows.sim.test_campaign_test_runs import (
    _manifest_run_groups,
    _v4_document,
    _work_item_test_runs,
)
from tests.flows.sim.test_campaign_test_runs import (
    _refinalized as _refinalized_test_runs,
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


@pytest.mark.parametrize(
    ("schema", "members"),
    [
        (_V2, {"coverage": True}),
        (_V2, {"coverage": True, "no_waivers": False}),
        (_V2, {"coverage": True, "no_waivers": "yes"}),
        (_V2, {"coverage": False, "no_waivers": True}),
        (_V1, {"coverage": True, "no_waivers": True}),
    ],
)
def test_no_waivers_manifest_grammar_is_exact(schema: str, members: dict[str, object]) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        _refinalized(schema, **members)


def test_v1_manifest_grammar_is_unchanged_by_no_waivers_support() -> None:
    manifest = _refinalized(_V1, coverage=True)
    assert manifest.document["$schema"] == _V1
    assert "no_waivers" not in manifest.document["workload"]  # type: ignore[operator]


@pytest.mark.parametrize("schema", ["v1", "v2", "v3"])
@pytest.mark.parametrize("mapping", [False, True])
def test_source_mapping_schema_grammar(schema, mapping):
    document = _legacy_manifest("v2" if schema == "v2" else "v1")
    document.pop("fingerprints")
    document["$schema"] = "booley.simulation-campaign-manifest/" + schema
    if schema == "v2":
        document["workload"]["coverage"] = True
        document["workload"]["no_waivers"] = True
        workload = document["workload"]
        from booley.flows.sim.campaign.flow_planning import _variant_document, _work_items

        variant = _variant_document(workload, [], False)
        variants, items = (
            [variant],
            _names_only_work_items(
                _work_items(
                    [(build_test_run(None),)],
                    document["target"],
                    variant,
                    "run",
                    "literal",
                    kind="ordinary_hdl",
                )
            ),
        )
        document["build_variants"], document["work_items"] = variants, items
    entry = {
        "path": "staged.hex",
        "bytes": 1,
        "sha256": "sha256:" + "a" * 64,
        "kind": "generated_input",
    }
    if mapping:
        entry["source_path"] = "data/source.hex"
    document["planning_disclosures"] = [_prepared_disclosure([entry])]
    if mapping and schema != "v3":
        with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
            finalize_manifest(document)
    else:
        manifest = finalize_manifest(document)
        assert (
            decode_simulation_campaign_manifest(encode_simulation_campaign_manifest(manifest))
            == manifest
        )


def test_a_manifest_before_v4_may_not_carry_test_runs(tmp_path: Path):
    document = _v4_document(tmp_path)
    document["$schema"] = "booley.simulation-campaign-manifest/v3"
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        _refinalized_test_runs(document)


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
