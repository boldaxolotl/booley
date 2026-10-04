"""Mutation fixtures must preserve state outside the declared disposable copy."""

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "qa_campaign_faults",
    Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults/campaign.py",
)
faults = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(faults)

NESTED = "campaign/coverage-campaign/coverage.json"


def _reference(target: Path, nested: Path) -> Path:
    """Write a Target reference to *nested*, as Booley publishes it."""
    raw = nested.read_bytes()
    reference = target / "coverage.json"
    reference.write_text(
        json.dumps(
            {
                "$schema": faults.REFERENCE_SCHEMA,
                "coverage_campaign": {
                    "path": NESTED,
                    "path_base": "origin_target",
                    "bytes": len(raw),
                    "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                },
            }
        )
    )
    return reference


def _owned_campaign(tmp_path: Path) -> tuple[Path, Path]:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    manifest = owned / NESTED
    manifest.parent.mkdir(parents=True)
    (manifest.parent / "coverage-points.jsonl.gz").write_bytes(b"points")
    manifest.write_text(
        json.dumps(
            {
                "$schema": "booley.coverage-campaign/v4",
                "collection": {"status": "complete"},
                "scoring": {"status": "valid", "reason": None},
                "rollups": [{"metric": "line"}],
                "source_rollups": [{"source": "rtl/a.sv", "rollups": []}],
            }
        )
    )
    return owned, manifest


@pytest.mark.parametrize(
    "mode", ["changed-points", "truncated-gzip", "duplicate-point", "symlink"]
)
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_linked_point_store_cannot_mutate_outside_sentinel(tmp_path, mode, link_kind):
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    manifest = owned / NESTED
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    reference = _reference(owned, manifest)
    sentinel = tmp_path / "borrowed"
    sentinel.write_bytes(b"preserve")
    points = manifest.parent / "coverage-points.jsonl.gz"
    try:
        if link_kind == "symlink":
            points.symlink_to(sentinel)
        else:
            os.link(sentinel, points)
    except OSError as error:
        pytest.skip(f"filesystem cannot create fixture {link_kind}: {error}")
    with pytest.raises(ValueError, match="mutation destination"):
        faults.mutate(reference, owned, mode)
    assert sentinel.read_bytes() == b"preserve"
    assert manifest.read_text() == "{}"


@pytest.mark.parametrize(
    ("mode", "retained", "cleared"),
    [
        ("invalid-overall-score", "rollups", "source_rollups"),
        ("invalid-source-score", "source_rollups", "rollups"),
    ],
)
def test_invalid_scoring_fault_retains_exact_smuggled_inventory(
    tmp_path: Path, mode: str, retained: str, cleared: str
) -> None:
    owned, manifest = _owned_campaign(tmp_path)

    faults.mutate(_reference(owned, manifest), owned, mode)

    document = json.loads(manifest.read_text())
    assert document["scoring"] == {"status": "invalid", "reason": "collector_error"}
    assert document["collection"]["status"] == "collector_error"
    assert document[retained]
    assert document[cleared] == []


@pytest.mark.parametrize("mode", ["duplicate-point", "invalid-final-record"])
def test_point_rewrite_is_canonical_and_rebinds_reference(tmp_path: Path, mode: str) -> None:
    """Rewritten points must pass Booley's canonical-line check to reach content validation."""
    owned = tmp_path / "owned"
    manifest = owned / NESTED
    manifest.parent.mkdir(parents=True)
    (owned / ".qa-coverage-fault-copy").touch()
    records = [
        {"$schema": "booley.coverage-points/v1", "campaign_id": "c"},
        {"id": "p1", "hits_by_run": {"run:001:half": 1}, "label": "ünïcode"},
    ]
    raw = b"".join(faults.canonical(row) for row in records)
    compressed = gzip.compress(raw, mtime=0)
    (manifest.parent / "coverage-points.jsonl.gz").write_bytes(compressed)
    manifest.write_text(
        json.dumps({"point_store": {"path": "coverage-points.jsonl.gz", "point_count": 1}})
    )
    reference = _reference(owned, manifest)

    faults.mutate(reference, owned, mode)

    lines = gzip.decompress((manifest.parent / "coverage-points.jsonl.gz").read_bytes())
    for line in lines.splitlines(keepends=True):
        compact = json.dumps(
            json.loads(line), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert line == compact.encode() + b"\n"
    bound = json.loads(reference.read_text())["coverage_campaign"]
    nested = manifest.read_bytes()
    assert bound["bytes"] == len(nested)
    assert bound["sha256"] == "sha256:" + hashlib.sha256(nested).hexdigest()


def test_mutate_refuses_a_nested_manifest_instead_of_the_reference(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    manifest = owned / "coverage.json"
    manifest.write_text(json.dumps({"$schema": "booley.coverage-campaign/v4"}))
    with pytest.raises(ValueError, match=r"Target coverage\.json"):
        faults.mutate(manifest, owned, "point-count")


@pytest.mark.parametrize(
    ("mode", "expected_codes"),
    [
        ("duplicate-point", {"COV_POINT_ID_DUPLICATE"}),
        ("invalid-final-record", {"COV_POINT_RUN_UNKNOWN", "COV_POINT_HIT_NONPOSITIVE"}),
    ],
)
def test_fault_reaches_product_content_validation(
    tmp_path: Path, mode: str, expected_codes: set[str]
) -> None:
    from tests.flows.sim.test_coverage_campaign_reference import (
        _nested_campaign,
    )
    from tests.flows.sim.test_coverage_campaign_reference import (
        _reference as product_reference,
    )

    from booley.flows.sim.coverage_campaign import CoverageCampaignValidationError
    from booley.flows.sim.coverage_reference import (
        publish_coverage_campaign_reference,
        resolve_coverage_campaign_reference,
    )

    owned = tmp_path / "owned"
    target = owned / "targets/sim_counter"
    manifest = _nested_campaign(target)
    reference = target / "coverage.json"
    publish_coverage_campaign_reference(reference, product_reference(target, manifest))
    (owned / ".qa-coverage-fault-copy").touch()
    resolve_coverage_campaign_reference(reference)

    faults.mutate(reference, owned, mode)

    with pytest.raises(CoverageCampaignValidationError) as caught:
        resolve_coverage_campaign_reference(reference)
    codes = {finding.code for finding in caught.value.findings}
    assert expected_codes <= codes
    assert "COV_POINT_CANONICAL" not in codes


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "outside"])
def test_nested_manifest_cannot_mutate_borrowed_file(tmp_path: Path, link_kind: str) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    sentinel = tmp_path / "borrowed.json"
    sentinel.write_text('{"preserve": true}')
    manifest = owned / NESTED
    manifest.parent.mkdir(parents=True)
    try:
        if link_kind == "symlink":
            manifest.symlink_to(sentinel)
        elif link_kind == "hardlink":
            os.link(sentinel, manifest)
        else:
            manifest = sentinel
    except OSError as error:
        pytest.skip(f"filesystem cannot create fixture {link_kind}: {error}")
    reference = _reference(owned, manifest)
    if link_kind == "outside":
        document = json.loads(reference.read_text())
        document["coverage_campaign"]["path"] = "../borrowed.json"
        reference.write_text(json.dumps(document))
    before = reference.read_bytes()

    with pytest.raises(ValueError, match="mutation destination"):
        faults.mutate(reference, owned, "point-count")

    assert sentinel.read_text() == '{"preserve": true}'
    assert reference.read_bytes() == before
