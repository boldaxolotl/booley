"""Byte-pin the build records each sharing scope writes.

The digests below were recorded from the serial executor at base commit
574e6e524, before shared and private builds were unified behind one record
builder. They must not change: records are durable record evidence. The bundle and
build-result digests were re-pinned once, for `booley.simulator-bundle/v3` (#4161):
the bundle document dropped `snapshot_inventory_sha256`. The attempt digests are unchanged.
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

from booley.flows.sim.record import codec, serial_execution
from booley.flows.sim.record.codec import canonical_json_bytes
from booley.flows.sim.record.model import SimulationResult
from booley.flows.sim.record.planning import finalize_manifest
from booley.flows.sim.record.store import SimulationRecordStore
from tests.flows.sim.simulation_record_test_support import (
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
        "sha256:c330e3f9de597bfd4d4a08059cc4def12f7dc11041ce94928996f3f4cae51320",
        "sha256:ef9cca5338b4f7e6f3084cc4ce661694b753689527771fd12b5258a94aa7539f",
        "sha256:67669837e562778da85b2e1d3f488841206f1acc9781b6871231a3074ee1a3e9",
    ),
    "legacy-per-test": (
        "private_work_item",
        "sha256:27dfcbe924ba12c50bc34cb5f58fe44b20e0147132b9476f282c3de34844cd03",
        "sha256:e2064e25174585087b96b6d9192a84fd06105e6de0964b48d9ebb334606da813",
        "sha256:0b9a04b6ba5bc7c5324a004ac0901dffbbaf80050f7bb3f7561f14fe53809f11",
    ),
}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _execute_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> tuple[SimulationRecordStore, SimulationResult]:
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
    store = SimulationRecordStore(tmp_path / "record")
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
    assert [name for name in decoded if name != "SimulationRecordManifest"] == []
