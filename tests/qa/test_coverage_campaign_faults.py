"""Mutation fixtures must preserve state outside the declared disposable copy."""

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


@pytest.mark.parametrize("mode", ["truncated-gzip", "duplicate-point", "symlink"])
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


def test_mutate_refuses_a_nested_manifest_instead_of_the_reference(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / ".qa-coverage-fault-copy").touch()
    manifest = owned / "coverage.json"
    manifest.write_text(json.dumps({"$schema": "booley.coverage-campaign/v5"}))
    with pytest.raises(ValueError, match=r"Target coverage\.json"):
        faults.mutate(manifest, owned, "wrong-evaluation")


def _product_reference(owned: Path, mode: str) -> Path:
    from tests.flows.sim.test_coverage_campaign_reference import _nested_campaign
    from tests.flows.sim.test_coverage_campaign_reference import _reference as product_reference

    from booley.flows.sim.coverage_reference import publish_coverage_campaign_reference

    target = owned / "targets/sim_counter"
    manifest = _nested_campaign(target)
    reference = target / "coverage.json"
    publish_coverage_campaign_reference(reference, product_reference(target, manifest))
    (owned / ".qa-coverage-fault-copy").touch()
    return reference


_PRODUCT_FAULTS = {
    "duplicate-point": {"COV_POINT_ID_DUPLICATE"},
    "invalid-final-record": {"COV_POINT_RUN_UNKNOWN", "COV_POINT_HIT_NONPOSITIVE"},
    "missing-points": {"COV_PATH_UNSAFE"},
    "truncated-gzip": {"COV_POINT_FORMAT"},
    "trailing-data": {"COV_POINT_FORMAT"},
    "symlink": {"COV_PATH_UNSAFE"},
    "wrong-rollup": {"COV_ROLLUP_MISMATCH"},
    "wrong-evaluation": {"COV_EVALUATION_MISMATCH"},
    "v1": {"COV_SCHEMA_VERSION_UNSUPPORTED"},
    "v2": {"COV_SCHEMA_VERSION_UNSUPPORTED"},
    "v3": {"COV_SCHEMA_VERSION_UNSUPPORTED"},
    "resource-ceiling": {"COV_POINT_LIMIT"},
}


def test_fault_reaches_product_content_validation(tmp_path: Path, monkeypatch) -> None:
    from tests.conftest import require_symlinks

    from booley.flows.sim import coverage_campaign_store as store
    from booley.flows.sim.coverage_campaign import CoverageCampaignValidationError
    from booley.flows.sim.coverage_campaign_store import CoverageCampaignStoreError
    from booley.flows.sim.coverage_reference import resolve_coverage_campaign_reference

    # One node, every mode; resource-ceiling is last because it patches the ceiling.
    for mode, expected_codes in _PRODUCT_FAULTS.items():
        if mode == "symlink":
            try:
                require_symlinks(tmp_path)
            except pytest.skip.Exception:
                continue  # only this mode needs symlinks; keep the others running
        owned = tmp_path / mode
        reference = _product_reference(owned, mode)
        resolve_coverage_campaign_reference(reference)
        if mode == "resource-ceiling":
            assert faults.POINT_CEILING_BYTES == store.MAX_UNCOMPRESSED_BYTES
            monkeypatch.setattr(store, "MAX_UNCOMPRESSED_BYTES", 4096)
            monkeypatch.setattr(faults, "POINT_CEILING_BYTES", 4096)

        faults.mutate(reference, owned, mode)

        with pytest.raises(
            (CoverageCampaignValidationError, CoverageCampaignStoreError)
        ) as caught:
            resolve_coverage_campaign_reference(reference)
        error = caught.value
        codes = (
            {finding.code for finding in error.findings}
            if isinstance(error, CoverageCampaignValidationError)
            else {error.code}
        )
        assert expected_codes <= codes


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
        faults.mutate(reference, owned, "wrong-evaluation")

    assert sentinel.read_text() == '{"preserve": true}'
    assert reference.read_bytes() == before
