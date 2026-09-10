"""V2 Coverage Campaign persistence through its public filesystem seam."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.flows.sim.coverage_campaign import (
    CoverageCampaignValidationError,
    DurableTargetIdentity,
    decode_coverage_campaign,
)
from booley.flows.sim.coverage_campaign_store import (
    MAX_COMPRESSED_BYTES,
    CoverageCampaignStoreError,
    load_coverage_campaign,
    publish_coverage_campaign,
    read_coverage_summary,
)
from tests.flows.sim.test_coverage_campaign import _valid_document

TARGET = DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")


def _campaign():
    return decode_coverage_campaign(_valid_document(), TARGET)


def test_publish_separates_summary_from_lossless_points(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())

    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    assert manifest["$schema"] == "booley.coverage-campaign/v2"
    assert "points" not in manifest
    assert manifest["rollups"][0]["percent"] == 100.0
    assert manifest["evaluation"]["status"] == "not_requested"
    assert manifest["point_store"]["path"] == "coverage-points.jsonl.gz"
    assert manifest["point_store"]["point_count"] == 1
    assert paths.points.is_file()

    summary = read_coverage_summary(paths.campaign, TARGET)
    loaded = load_coverage_campaign(paths.campaign, TARGET)
    assert summary.rollups[0].percent == 100.0
    assert summary.evaluation["status"] == "not_requested"
    assert loaded.summary == summary
    assert loaded.campaign == _campaign()
    assert not hasattr(loaded.campaign, "schema")


def test_summary_read_does_not_require_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    paths.points.unlink()

    summary = read_coverage_summary(paths.campaign, TARGET)

    assert summary.rollups[0].percent == 100.0
    with pytest.raises(CoverageCampaignStoreError, match="missing or unsafe"):
        load_coverage_campaign(paths.campaign, TARGET)


def test_publish_never_replaces_existing_campaign_artifact(tmp_path: Path) -> None:
    existing = tmp_path / "coverage-points.jsonl.gz"
    existing.write_bytes(b"owned")

    with pytest.raises(CoverageCampaignStoreError, match="cannot replace"):
        publish_coverage_campaign(tmp_path, _campaign())

    assert existing.read_bytes() == b"owned"


def test_publish_never_replaces_existing_manifest(tmp_path: Path) -> None:
    existing = tmp_path / "coverage.json"
    existing.write_bytes(b"owned")

    with pytest.raises(CoverageCampaignStoreError, match="cannot replace"):
        publish_coverage_campaign(tmp_path, _campaign())

    assert existing.read_bytes() == b"owned"
    assert not (tmp_path / "coverage-points.jsonl.gz").exists()


def test_deep_reader_rejects_tampered_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    stored = bytearray(paths.points.read_bytes())
    stored[-1] ^= 1
    paths.points.write_bytes(stored)

    assert read_coverage_summary(paths.campaign, TARGET).rollups[0].percent == 100.0
    with pytest.raises(CoverageCampaignStoreError):
        load_coverage_campaign(paths.campaign, TARGET)


def test_summary_rejects_resource_metadata_above_format_limit(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    manifest["point_store"]["bytes"] = MAX_COMPRESSED_BYTES + 1
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")
    paths.points.unlink()

    with pytest.raises(CoverageCampaignStoreError, match="exceeds V2 limit"):
        read_coverage_summary(paths.campaign, TARGET)


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup requires host privileges")
def test_deep_reader_rejects_linked_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    retained = tmp_path / "retained.gz"
    paths.points.rename(retained)
    paths.points.symlink_to(retained.name)

    with pytest.raises(CoverageCampaignStoreError, match="missing or unsafe"):
        load_coverage_campaign(paths.campaign, TARGET)


def test_summary_read_rejects_invalid_stored_evaluation_without_opening_points(
    tmp_path: Path,
) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    manifest["evaluation"]["status"] = "pass"
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")
    paths.points.unlink()

    with pytest.raises(CoverageCampaignValidationError, match="invalid"):
        read_coverage_summary(paths.campaign, TARGET)


def test_deep_reader_preserves_retained_v1_campaigns(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_valid_document()), encoding="utf-8")

    loaded = load_coverage_campaign(path, TARGET)

    assert loaded.summary.source_schema == "booley.coverage-campaign/v1"
    assert loaded.summary.point_store is None
    assert loaded.campaign == _campaign()
