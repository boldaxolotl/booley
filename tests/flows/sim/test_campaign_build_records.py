"""Byte-pin the build records each sharing scope writes.

The digests below were recorded from the serial executor at base commit
574e6e524, before shared and private builds were unified behind one record
builder. They must not change: records are durable campaign evidence.
It also checks that no builder decodes the record it just built.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim.campaign import codec, serial_execution
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.model import SimulationResult
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.store import CampaignStore
from tests.flows.sim.campaign_test_support import (
    _executor,
    _failed_shared_outcome,
    _Group,
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


def _execute_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> tuple[CampaignStore, SimulationResult]:
    """Run one work item through the serial executor on *route* with frozen ids and clock."""
    sequence = iter(range(1, 100))
    # ``version=4`` sets the version and variant bits the codec requires.
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(sequence), version=4))
    monkeypatch.setattr(serial_execution, "_now", lambda: "2026-09-21T10:00:00Z")
    monkeypatch.setattr(serial_execution, "time", SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    if route == "shared-design-failure":
        failed = _failed_shared_outcome()
        failed = replace(failed, tests=(replace(failed.tests[0], name="alpha"),))
        monkeypatch.setattr(_Group, "finish_build_failure", lambda self: failed, raising=False)
        monkeypatch.setattr(_Group, "compile", lambda self: SimpleNamespace(passed=False))
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
    return store, result


@pytest.mark.parametrize("route", ["shared", "legacy-per-test"])
def test_build_records_are_byte_identical_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    store, result = _execute_route(tmp_path, monkeypatch, route)

    (build_result,) = store.root.rglob("build-result.json")
    directory = build_result.parent
    sharing, attempt_digest, bundle_digest, result_digest = _PINNED_RECORDS[route]
    assert result.document["build_result"]["sharing"] == sharing
    assert (
        _digest(directory / "build-attempt.json"),
        _digest(directory / "evidence" / "bundle.json"),
        _digest(build_result),
    ) == (attempt_digest, bundle_digest, result_digest)


@pytest.mark.parametrize(
    ("route", "build_state"),
    [
        ("shared", "ready"),
        ("legacy-per-test", "ready"),
        ("shared-design-failure", "design_failure"),
    ],
)
def test_record_builders_decode_no_record_they_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, build_state: str
) -> None:
    decoded: list[str] = []
    real_decode = codec._decode

    def spy(raw: bytes, **kwargs: Any) -> Any:
        decoded.append(kwargs["value_type"].__name__)
        return real_decode(raw, **kwargs)

    monkeypatch.setattr(codec, "_decode", spy)

    store, _result = _execute_route(tmp_path, monkeypatch, route)

    # The route reached its build record, so a skipped builder cannot pass unnoticed.
    (build_result,) = store.root.rglob("build-result.json")
    assert json.loads(build_result.read_bytes())["state"] == build_state
    # ``finalize_manifest`` decodes the manifest once as planning's validation boundary.
    assert [name for name in decoded if name != "SimulationCampaignManifest"] == []
