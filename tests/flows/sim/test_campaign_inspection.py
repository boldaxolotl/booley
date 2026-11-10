"""Public inspection and corrupt-record readers of Simulation Campaigns."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from booley.flows.sim.campaign import (
    SimulationCampaignWorkItemError,
    authenticate_work_item,
    inspect_retained_campaign,
)
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.store import CampaignStore
from tests.flows.sim.campaign_test_support import (
    _completed,
    _executor,
    _hook_publication_request,
    _manifest_for,
    _maximum_hook_selection_manifest,
    _observe_pre_sim_reads,
    _publish_request_hook,
    _request,
)


def test_public_campaign_inspection_returns_typed_evidence_without_writes(
    tmp_path: Path,
) -> None:
    completed = _completed(tmp_path)
    completed.store.regenerate_summary()
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in completed.store.root.rglob("*")
        if path.is_file()
    }

    evidence = authenticate_work_item(completed.store.manifest_path, completed.item_id)
    status = inspect_retained_campaign(completed.store.manifest_path)

    assert evidence.manifest_path == completed.store.manifest_path
    assert evidence.manifest_sha256 == completed.store.manifest_sha256()
    assert evidence.work_item_id == completed.item_id
    assert evidence.work_item["work_item_id"] == completed.item_id
    assert evidence.result.document["work_item_id"] == completed.item_id
    assert status.manifest_path == completed.store.manifest_path
    assert status.summary_path == completed.store.summary_path
    assert status.completed == (completed.item_id,)
    assert status.pending == status.interrupted == ()
    assert status.summary_complete
    assert status.summary_completed_matches
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before


def test_public_work_item_authentication_rejects_unknown_and_unfinished_items(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    item = cast(tuple[Mapping[str, object], ...], store.load_manifest().document["work_items"])[0]
    work_item_id = cast(str, item["work_item_id"])

    with pytest.raises(SimulationCampaignWorkItemError, match="no exact terminal"):
        authenticate_work_item(store.manifest_path, "item:9999:0000000000000000")
    with pytest.raises(SimulationCampaignWorkItemError, match="no exact terminal"):
        authenticate_work_item(store.manifest_path, work_item_id)


def test_public_campaign_inspection_distinguishes_recovery_and_summary_status(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    store.regenerate_summary()
    pending = inspect_retained_campaign(store.manifest_path)
    assert pending.pending and not pending.interrupted
    assert not pending.summary_complete and pending.summary_completed_matches

    work_item_id = pending.pending[0]
    store.allocate_attempt_directory(work_item_id, str(uuid.uuid4()))
    store.regenerate_summary()
    interrupted = inspect_retained_campaign(store.manifest_path)
    assert interrupted.interrupted == (work_item_id,)
    assert not interrupted.pending


def test_public_campaign_inspection_reports_an_unpublished_summary(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))

    status = inspect_retained_campaign(store.manifest_path)

    assert status.pending
    assert not status.summary_present
    assert not status.summary_complete
    assert status.summary_completed_matches


def test_public_campaign_inspection_rejects_malformed_summary_but_reports_mismatch(
    tmp_path: Path,
) -> None:
    completed = _completed(tmp_path)
    completed.store.regenerate_summary()
    summary = json.loads(completed.store.summary_path.read_bytes())
    summary["complete"] = False
    completed.store.summary_path.write_bytes(canonical_json_bytes(summary))

    status = inspect_retained_campaign(completed.store.manifest_path)
    assert not status.summary_complete
    assert status.summary_completed_matches

    summary["complete"] = True
    summary["completed"] = []
    completed.store.summary_path.write_bytes(canonical_json_bytes(summary))
    status = inspect_retained_campaign(completed.store.manifest_path)
    assert status.summary_complete
    assert not status.summary_completed_matches

    completed.store.summary_path.write_bytes(canonical_json_bytes([]))
    with pytest.raises(SimulationCampaignIntegrityError, match="not a JSON object"):
        inspect_retained_campaign(completed.store.manifest_path)


def test_public_work_item_authentication_rejects_file_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = _completed(tmp_path)
    manifest = completed.store.manifest_path
    original_read = os.read
    swapped = False

    def swap_after_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        raw = original_read(descriptor, count)
        if not swapped:
            swapped = True
            if os.name == "nt":  # Windows refuses to rename an open file: extend it instead.
                with manifest.open("ab") as stream:
                    stream.write(b"\n")
                return raw
            replacement = manifest.with_name("original-manifest.json")
            manifest.rename(replacement)
            manifest.write_bytes(replacement.read_bytes())
        return raw

    monkeypatch.setattr(os, "read", swap_after_read)

    with pytest.raises(SimulationCampaignIntegrityError, match="changed during read"):
        authenticate_work_item(manifest, completed.item_id)


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
def test_summary_reader_rejects_corrupt_record(tmp_path, error):
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    store.regenerate_summary()
    document = dict(store.load_summary())
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document["complete"]
    store.summary_path.write_bytes(
        b"{" if error == "truncated" else canonical_json_bytes(document)
    )
    with pytest.raises(SimulationCampaignIntegrityError):
        store.load_summary()


@pytest.mark.timeout(180)
def test_maximum_selection_uses_large_sidecar_and_bounded_report_preview(tmp_path):
    from booley.flows.sim.campaign import read_pre_sim_firings
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.flow import _pre_sim_projection

    names = tuple(f"test{index:04d}" + "x" * 504 for index in range(4000))

    manifest, _disclosures = _maximum_hook_selection_manifest(names)
    invocation = tmp_path / "reports/1"
    store = CampaignStore(invocation / "targets/sim/campaign")
    store.publish_manifest(manifest)
    request = _request(store, manifest, manifest.document["work_items"][0], tmp_path, invocation=1)
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    evidence = PreSimEvidence(
        ("true",),
        names,
        "passed",
        0.1,
        returncode=0,
        stdout_tail="s" * 8192,
        stderr_tail="e" * 8192,
    )
    reference = publish_pre_sim_firing(request, evidence)
    assert 1024 * 1024 < reference["bytes"] < 16 * 1024 * 1024
    assert len(canonical_json_bytes(reference)) < 1024
    firings = read_pre_sim_firings(store.manifest_path)
    assert firings[0].document["test_names"] == names
    detail, _card = _pre_sim_projection(
        {f.key: f for f in firings}, frozenset(f.key for f in firings), invocation
    )
    assert len(json.dumps(detail["pre_sim_runs"]).encode()) <= 16 * 1024
    assert detail["pre_sim_runs"][0]["test_count"] == 4000
    assert len(detail["pre_sim_runs"][0]["test_names"]) == 8


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
def test_pre_sim_reader_rejects_corrupt_record(tmp_path, error):
    request = _hook_publication_request(tmp_path)
    _publish_request_hook(request)
    assert len(request.store.read_pre_sim_firings()) == 1
    path = request.attempt_directory / "pre-sim/0001.json"
    document = json.loads(path.read_bytes())
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document["attempt_id"]
    path.chmod(0o600)
    path.write_bytes(b"{" if error == "truncated" else canonical_json_bytes(document))
    with pytest.raises(SimulationCampaignIntegrityError):
        request.store.read_pre_sim_firings()


def test_pre_sim_inspection_reads_fresh(tmp_path, monkeypatch):
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    store = CampaignStore(invocation / "targets/sim/campaign")

    def inspect():
        assert store.inspect_pre_sim_firings() == outcome.pre_sim_firings

    accesses, _ = _observe_pre_sim_reads(monkeypatch)
    inspect()
    first = accesses[firing.path]
    assert first > 0
    inspect()
    assert accesses[firing.path] == 2 * first
    firing.path.write_bytes(firing.path.read_bytes() + b" ")
    with pytest.raises(SimulationCampaignIntegrityError):
        inspect()


@pytest.mark.parametrize(
    "defect, value, message",
    [
        ("attempt_id", "wrong-owner", "evidence owner disagrees"),
        ("ordinal", True, "ordinal"),
        pytest.param("elapsed_s", float("nan"), "elapsed_s", marks=pytest.mark.timeout(120)),
        ("returncode", True, "returncode"),
        pytest.param("command_count", 3, "command count", marks=pytest.mark.timeout(150)),
        ("test_names", ["other"], "selection disagrees"),
        ("stdout_tail", "x" * 8193, "over-budget stdout_tail"),
        ("status", "invented", "status"),
        ("bytes", None, "result references disagree"),
        ("sidecar-link", None, "regular file|link"),
        ("ordinal-path", None, "ordinal disagrees"),
        ("directory-owner", None, "directory owner disagrees"),
    ],
    ids=lambda value: None if value is None else str(value)[:32],
)
def test_pre_sim_read_identity_and_reference_cases(tmp_path, monkeypatch, defect, value, message):
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    result_path = firing.path.parents[3] / "result.json"
    if value is not None:
        document = json.loads(firing.path.read_bytes())
        document[defect] = value
        firing.path.write_text(json.dumps(document))
        result_path.unlink()
    elif defect == "bytes":
        firing.path.write_bytes(firing.path.read_bytes() + b" ")
    elif defect == "sidecar-link":
        firing.path.unlink()
        firing.path.symlink_to(tmp_path / "outside.json")
    elif defect == "missing-reference":
        document = json.loads(result_path.read_bytes())
        document["evidence"] = [
            ref for ref in document["evidence"] if ref["kind"] != "pre_sim_commands"
        ]
        result_path.write_bytes(canonical_json_bytes(document))
    else:
        result_path.unlink()
        if defect == "ordinal-path":
            firing.path.rename(firing.path.with_name("0002.json"))
        else:
            directory = firing.path.parent.parent
            directory.rename(directory.with_name("0001-other-owner"))
    store = CampaignStore(invocation / "targets/sim/campaign")
    with pytest.raises(SimulationCampaignIntegrityError, match=message):
        store.scan()


def _pre_sim_read_case(tmp_path, monkeypatch, *, interrupted=False):
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    store = CampaignStore(invocation / "targets/sim/campaign")
    if interrupted:
        (firing.path.parents[3] / "result.json").unlink()
    expected = store.scan()
    accesses, decodes = _observe_pre_sim_reads(monkeypatch)
    return store, firing, expected, accesses, decodes


def test_scan_validates_each_pre_sim_snapshot_once(tmp_path, monkeypatch):
    store, firing, expected, accesses, decodes = _pre_sim_read_case(tmp_path, monkeypatch)
    assert store.scan() == expected
    assert accesses == {firing.path: 1}, "each sidecar must have one byte access"
    assert decodes == {(firing.key[2], firing.key[3]): 1}, "each sidecar must be decoded once"


@pytest.mark.parametrize(
    "entry, interrupted",
    [("retained", False), ("scan", True), ("retained", True)],
    ids=["complete-retained", "interrupted-scan", "interrupted-retained"],
)
def test_other_inspections_validate_each_pre_sim_snapshot_once(
    tmp_path, monkeypatch, entry, interrupted
):
    from booley.flows.sim.campaign.inspection import inspect_retained_campaign

    store, firing, expected, accesses, decodes = _pre_sim_read_case(
        tmp_path, monkeypatch, interrupted=interrupted
    )
    if entry == "scan":
        assert store.scan() == expected
    else:
        status = inspect_retained_campaign(store.manifest_path)
        assert status.completed == expected.complete
        assert status.interrupted == expected.interrupted
        assert firing.path.absolute() in status.retention_files
    assert accesses == {firing.path: 1}, "each sidecar must have one byte access"
    assert decodes == {(firing.key[2], firing.key[3]): 1}, "each sidecar must be decoded once"
