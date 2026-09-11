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
    CAMPAIGN_SCHEMA_V3,
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


def _point(
    *, source: str, metric: str, line: int, hits: int, disposition: str, hierarchy: str
) -> CoveragePoint:
    identity = CoveragePointIdentity(
        metric=metric,
        location=freeze_coverage_mapping(
            {
                "source": source,
                "start": {"line": line, "column": 1},
                "end": {"line": line, "column": 2},
            }
        ),
        hierarchy=hierarchy,
        subject=freeze_coverage_mapping({"outcome": line}),
        collector=freeze_coverage_mapping(
            {"record_type": f"v_{metric}", "native_key": f"{source}:{metric}:{line}"}
        ),
    )
    point = CoveragePoint(
        "pending",
        identity,
        {"run:reset": hits} if hits else {},
        freeze_coverage_mapping({"kind": disposition}),
    )
    encoded_identity = encode_coverage_point(point)["identity"]
    assert isinstance(encoded_identity, dict)
    return replace(point, id=_point_id(encoded_identity))


def _source_campaign():
    base = _campaign()
    points = (
        _point(
            source="rtl/z.sv",
            metric="line",
            line=1,
            hits=1,
            disposition="eligible",
            hierarchy="TOP.first",
        ),
        _point(
            source="rtl/z.sv",
            metric="line",
            line=2,
            hits=0,
            disposition="waived",
            hierarchy="TOP.second",
        ),
        _point(
            source="rtl/a.sv",
            metric="branch",
            line=3,
            hits=0,
            disposition="eligible",
            hierarchy="TOP.third",
        ),
        _point(
            source="tb/bench.sv",
            metric="expression",
            line=4,
            hits=1,
            disposition="unscored",
            hierarchy="TOP.tb",
        ),
        _point(
            source="rtl/a.sv",
            metric="toggle",
            line=5,
            hits=2,
            disposition="eligible",
            hierarchy="TOP.fourth",
        ),
        _point(
            source="rtl/a.sv",
            metric="cover_property",
            line=6,
            hits=1,
            disposition="eligible",
            hierarchy="TOP.fifth",
        ),
    )
    return replace(base, points=points, rollups=derive_coverage_rollups(points))


def _two_source_line_campaign():
    base = _campaign()
    points = (
        _point(
            source="rtl/counter.sv",
            metric="line",
            line=10,
            hits=1,
            disposition="eligible",
            hierarchy="TOP.first",
        ),
        _point(
            source="tb/counter_tb.sv",
            metric="line",
            line=11,
            hits=0,
            disposition="unscored",
            hierarchy="TOP.second",
        ),
    )
    return replace(base, points=points, rollups=derive_coverage_rollups(points))


def test_publish_separates_summary_from_lossless_points(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())

    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    assert manifest["$schema"] == CAMPAIGN_SCHEMA_V3
    assert "points" not in manifest
    assert manifest["rollups"][0]["percent"] == 100.0
    assert manifest["evaluation"]["status"] == "not_requested"
    assert manifest["point_store"]["path"] == "coverage-points.jsonl.gz"
    assert manifest["point_store"]["point_count"] == 1
    assert [item["source"] for item in manifest["source_rollups"]] == ["rtl/counter.sv"]
    assert [item["metric"] for item in manifest["source_rollups"][0]["rollups"]] == [
        "line",
        "branch",
        "expression",
        "toggle",
    ]
    assert manifest["source_rollups"][0]["rollups"][0]["percent"] == 100.0
    assert paths.points.is_file()

    summary = read_coverage_summary(paths.campaign, TARGET)
    loaded = load_coverage_campaign(paths.campaign, TARGET)
    assert summary.rollups[0].percent == 100.0
    assert summary.source_rollups[0].source == "rtl/counter.sv"
    assert summary.evaluation["status"] == "not_requested"
    assert loaded.summary == summary
    assert loaded.campaign == _campaign()
    assert not hasattr(loaded.campaign, "schema")


def test_manifest_has_canonical_per_source_rollups(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _source_campaign())
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))

    assert [item["source"] for item in manifest["source_rollups"]] == [
        "rtl/a.sv",
        "rtl/z.sv",
        "tb/bench.sv",
    ]
    assert all(
        [rollup["metric"] for rollup in source["rollups"]]
        == ["line", "branch", "expression", "toggle"]
        for source in manifest["source_rollups"]
    )
    by_source = {item["source"]: item["rollups"] for item in manifest["source_rollups"]}
    z_line = by_source["rtl/z.sv"][0]
    assert z_line == {
        "metric": "line",
        "semantics": (
            "One Verilator basic-block point; covered when its count is greater than zero."
        ),
        "total_points": 2,
        "eligible_points": 1,
        "covered_points": 1,
        "waived_points": 1,
        "percent": 100.0,
    }
    assert by_source["rtl/a.sv"][1]["percent"] == 0.0
    assert by_source["rtl/a.sv"][3]["percent"] == 100.0
    assert by_source["tb/bench.sv"][2]["total_points"] == 1
    assert by_source["tb/bench.sv"][2]["eligible_points"] == 0
    assert by_source["tb/bench.sv"][2]["percent"] is None
    assert all(
        rollup["metric"] != "cover_property"
        for source in manifest["source_rollups"]
        for rollup in source["rollups"]
    )


def test_deep_reader_rejects_source_distribution_tamper(tmp_path: Path) -> None:
    campaign = _two_source_line_campaign()
    paths = publish_coverage_campaign(tmp_path, campaign)
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    by_source = {item["source"]: item for item in manifest["source_rollups"]}
    rtl_line = by_source["rtl/counter.sv"]["rollups"][0]
    tb_line = by_source["tb/counter_tb.sv"]["rollups"][0]
    rtl_line.update(
        total_points=0, eligible_points=0, covered_points=0, waived_points=0, percent=None
    )
    tb_line.update(
        total_points=2, eligible_points=1, covered_points=1, waived_points=0, percent=100.0
    )
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")

    read_coverage_summary(paths.campaign, TARGET)
    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(paths.campaign, TARGET)

    assert error.value.code == "COV_SOURCE_ROLLUP_MISMATCH"


def test_summary_reader_rejects_source_semantics_tamper(tmp_path: Path) -> None:
    paths = publish_coverage_campaign(tmp_path, _campaign())
    manifest = json.loads(paths.campaign.read_text(encoding="utf-8"))
    manifest["source_rollups"][0]["rollups"][0]["semantics"] = "tampered"
    paths.campaign.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CoverageCampaignStoreError) as error:
        read_coverage_summary(paths.campaign, TARGET)

    assert error.value.code == "COV_SOURCE_ROLLUP_MISMATCH"


def test_publish_rejects_oversized_manifest_and_removes_point_store(
    tmp_path: Path, monkeypatch
) -> None:
    import booley.flows.sim.coverage_campaign_store as store

    monkeypatch.setattr(store, "MAX_MANIFEST_BYTES", 1)

    with pytest.raises(CoverageCampaignStoreError) as error:
        publish_coverage_campaign(tmp_path, _campaign())

    assert error.value.code == "COV_MANIFEST_LIMIT"
    assert not (tmp_path / "coverage.json").exists()
    assert not (tmp_path / "coverage-points.jsonl.gz").exists()


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


@pytest.mark.parametrize("reader", [read_coverage_summary, load_coverage_campaign])
@pytest.mark.parametrize("schema", ["booley.coverage-campaign/v1", "booley.coverage-campaign/v2"])
def test_readers_reject_pre_v3_campaigns(tmp_path: Path, reader, schema: str) -> None:
    path = tmp_path / "coverage.json"
    document = _valid_document()
    document["$schema"] = schema
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CoverageCampaignStoreError, match="recollect coverage") as error:
        reader(path, TARGET)

    assert error.value.code == "COV_SCHEMA_VERSION_UNSUPPORTED"


def test_summary_reader_rejects_unsupported_schema(tmp_path: Path) -> None:
    document = _valid_document()
    document["$schema"] = "booley.coverage-campaign/v999"
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CoverageCampaignStoreError) as error:
        read_coverage_summary(path, TARGET)

    assert error.value.code == "COV_SCHEMA_VERSION_UNSUPPORTED"


def test_unsupported_schema_is_rejected_before_campaign_decode(
    tmp_path: Path, monkeypatch
) -> None:
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

    with pytest.raises(CoverageCampaignStoreError) as error:
        load_coverage_campaign(path, TARGET)

    assert error.value.code == "COV_SCHEMA_VERSION_UNSUPPORTED"
    assert calls == 0


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
