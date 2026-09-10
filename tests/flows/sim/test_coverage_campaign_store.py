"""V2 Coverage Campaign persistence through its public filesystem seam."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.coverage_campaign import (
    CoverageCampaignValidationError,
    CoveragePoint,
    CoveragePointIdentity,
    DurableTargetIdentity,
    decode_coverage_campaign,
    derive_coverage_rollups,
    encode_coverage_point,
    freeze_coverage_mapping,
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


def _rewrite_point_store(paths, data: bytes) -> None:
    compressed = gzip.compress(data, mtime=0)
    paths.points.write_bytes(compressed)
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    manifest["point_store"].update(
        sha256=f"sha256:{hashlib.sha256(compressed).hexdigest()}",
        bytes=len(compressed),
        uncompressed_bytes=len(data),
    )
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")


def _point_id(identity: dict[str, object]) -> str:
    data = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return f"cp1:{base64.urlsafe_b64encode(data).decode().rstrip('=')}"


def _large_campaign(point_count: int):
    base = _campaign()
    points = []
    for index in range(point_count):
        identity = CoveragePointIdentity(
            metric="line",
            location=freeze_coverage_mapping(
                {
                    "source": "rtl/counter.sv",
                    "start": {"line": index + 1, "column": 1},
                    "end": {"line": index + 1, "column": 2},
                }
            ),
            hierarchy="TOP.counter",
            subject=freeze_coverage_mapping({"basic_block": index}),
            collector=freeze_coverage_mapping(
                {"record_type": "v_line", "native_key": f"line:{index}"}
            ),
        )
        point = CoveragePoint(
            "pending",
            identity,
            {"run:reset": 1},
            freeze_coverage_mapping({"kind": "eligible"}),
        )
        encoded_identity = encode_coverage_point(point)["identity"]
        assert isinstance(encoded_identity, dict)
        points.append(replace(point, id=_point_id(encoded_identity)))
    frozen = tuple(points)
    return replace(base, points=frozen, rollups=derive_coverage_rollups(frozen))


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
    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(paths.campaign, TARGET)
    assert error.value.code == "COV_PATH_UNSAFE"


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
    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(paths.campaign, TARGET)
    assert error.value.code in {"COV_POINT_FORMAT", "COV_POINT_INTEGRITY"}


def test_summary_rejects_resource_metadata_above_format_limit(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    manifest["point_store"]["bytes"] = MAX_COMPRESSED_BYTES + 1
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")
    paths.points.unlink()

    with pytest.raises(CoverageCampaignStoreError, match="exceeds V2 limit") as error:
        read_coverage_summary(paths.campaign, TARGET)
    assert error.value.code == "COV_POINT_LIMIT"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup requires host privileges")
def test_deep_reader_rejects_linked_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    retained = tmp_path / "retained.gz"
    paths.points.rename(retained)
    paths.points.symlink_to(retained.name)

    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(paths.campaign, TARGET)
    assert error.value.code == "COV_PATH_UNSAFE"


def test_deep_reader_rejects_noncanonical_jsonl(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    lines = gzip.decompress(paths.points.read_bytes()).splitlines(keepends=True)
    point = json.loads(lines[1])
    lines[1] = json.dumps(point, separators=(", ", ": ")).encode() + b"\n"
    _rewrite_point_store(paths, b"".join(lines))

    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(paths.campaign, TARGET)

    assert error.value.code == "COV_POINT_CANONICAL"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup requires host privileges")
def test_manifest_reader_rejects_linked_parent(tmp_path: Path) -> None:
    retained = tmp_path / "retained"
    paths = publish_coverage_campaign(retained, _campaign())
    linked = tmp_path / "linked"
    linked.symlink_to(retained, target_is_directory=True)

    with pytest.raises(CoverageCampaignStoreError) as error:
        read_coverage_summary(linked / paths.campaign.name, TARGET)

    assert error.value.code == "COV_PATH_UNSAFE"


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


def test_v1_deep_reader_decodes_once(tmp_path: Path, monkeypatch) -> None:
    import booley.flows.sim.coverage_campaign_store as store

    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_valid_document()), encoding="utf-8")
    decode = store.decode_coverage_campaign
    calls = 0

    def counted_decode(document, expected_target):
        nonlocal calls
        calls += 1
        return decode(document, expected_target)

    monkeypatch.setattr(store, "decode_coverage_campaign", counted_decode)

    load_coverage_campaign(path, TARGET)

    assert calls == 1


def test_large_campaign_keeps_manifest_and_summary_work_constant(tmp_path: Path) -> None:
    small = publish_coverage_campaign(tmp_path / "small", _campaign())
    large_campaign = _large_campaign(40_000)
    large = publish_coverage_campaign(tmp_path / "large", large_campaign)
    small_manifest = small.campaign.read_bytes()
    large_manifest = large.campaign.read_bytes()

    summary = read_coverage_summary(large.campaign, TARGET)

    assert len(large_manifest) - len(small_manifest) < 128
    assert summary.point_store is not None
    assert summary.point_store.point_count == 40_000
    assert large.points.stat().st_size < summary.point_store.uncompressed_bytes
