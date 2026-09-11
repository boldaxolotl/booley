"""Mutation fixtures must preserve state outside the declared disposable copy."""

import importlib.util
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "qa_campaign_faults",
    Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults/campaign.py",
)
faults = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(faults)


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
