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


@pytest.mark.parametrize("mode", ["duplicate-point", "invalid-final-record"])
def test_point_rewrite_is_canonical_and_rebinds_reference(tmp_path, mode):
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


def test_mutate_refuses_a_nested_manifest_instead_of_the_reference(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    manifest = owned / "coverage.json"
    manifest.write_text(json.dumps({"$schema": "booley.coverage-campaign/v3"}))
    with pytest.raises(ValueError, match=r"Target coverage\.json"):
        faults.mutate(manifest, owned, "point-count")
