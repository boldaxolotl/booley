"""Mutation fixtures must preserve state outside the declared disposable copy."""

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


def _owned_campaign(tmp_path: Path) -> tuple[Path, Path]:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    (owned / "coverage-points.jsonl.gz").write_bytes(b"points")
    manifest = owned / "coverage.json"
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
    manifest = owned / "coverage.json"
    manifest.write_text("{}")
    sentinel = tmp_path / "borrowed"
    sentinel.write_bytes(b"preserve")
    points = owned / "coverage-points.jsonl.gz"
    try:
        if link_kind == "symlink":
            points.symlink_to(sentinel)
        else:
            os.link(sentinel, points)
    except OSError as error:
        pytest.skip(f"filesystem cannot create fixture {link_kind}: {error}")
    with pytest.raises(ValueError, match="mutation destination"):
        faults.mutate(manifest, owned, mode)
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

    faults.mutate(manifest, owned, mode)

    document = json.loads(manifest.read_text())
    assert document["scoring"] == {"status": "invalid", "reason": "collector_error"}
    assert document["collection"]["status"] == "collector_error"
    assert document[retained]
    assert document[cleared] == []
