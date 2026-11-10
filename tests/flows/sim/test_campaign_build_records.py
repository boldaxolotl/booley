"""Byte-pin the build records each sharing scope writes.

The digests below were recorded from the serial executor at base commit
574e6e524, before shared and private builds were unified behind one record
builder. They must not change: records are durable campaign evidence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import serial_execution
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.store import CampaignStore
from tests.flows.sim.campaign_test_support import (
    _executor,
    _legacy_disclosures,
    _manifest_for,
    _request,
)

#: ``(sharing, digest of build-attempt.json, evidence/bundle.json, build-result.json)``
_PINNED_RECORDS = {
    "shared": (
        "shared_variant",
        "sha256:9e2d46a84b0b683e5b33d3b75ee92839199eb3c010dd6a77631a5ff6feacf415",
        "sha256:03c518c2b7415509471f092f86d0b1c5e40407ef907ac3df5945958a756dd847",
        "sha256:526e80c53df7d80c28b74a0a45a6385f23cecf9846c931b982e8057aa314f24e",
    ),
    "legacy-per-test": (
        "private_work_item",
        "sha256:846aa8a5ee8d4b5e7bac4edfd0d40168bc2a5f071f84534e092ca4375db1429c",
        "sha256:a671f0734ac243bdea65a085a6649266128ffd56b1de21ec130d99508801eebe",
        "sha256:9970bfad180465db492b57043990f9d0b53b1bbb830859346b748ceb4707d5e1",
    ),
}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("route", ["shared", "legacy-per-test"])
def test_build_records_are_byte_identical_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    sequence = iter(range(1, 100))
    # ``version=4`` sets the version and variant bits the codec requires.
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(sequence), version=4))
    monkeypatch.setattr(serial_execution, "_now", lambda: "2026-09-21T10:00:00Z")
    monkeypatch.setattr(serial_execution, "time", SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    disclosures = _legacy_disclosures() if route == "legacy-per-test" else None
    manifest = _manifest_for(("alpha",), access=route if disclosures else "immutable")
    if disclosures:
        document = json.loads(canonical_json_bytes(manifest.document))
        document.pop("fingerprints")
        document["planning_disclosures"] = [disclosures["alpha"]]
        manifest = finalize_manifest(document)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    item = manifest.document["work_items"][0]
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    result = _executor(build_root, counters, disclosures=disclosures).execute(
        _request(store, manifest, item, project, invocation=1)
    )

    (build_result,) = store.root.rglob("build-result.json")
    directory = build_result.parent
    sharing, attempt_digest, bundle_digest, result_digest = _PINNED_RECORDS[route]
    assert result.document["build_result"]["sharing"] == sharing
    assert (
        _digest(directory / "build-attempt.json"),
        _digest(directory / "evidence" / "bundle.json"),
        _digest(build_result),
    ) == (attempt_digest, bundle_digest, result_digest)
