"""Tests for the per-Ticket Waiver Candidate store (ADR 0066)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import booley
from booley.core.boundary import BoundaryError
from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.flows.sim import waiver_candidates as store
from booley.flows.sim.waiver_candidates import (
    CampaignBinding,
    CandidateRecord,
    Rejection,
    WaiverCandidateLockError,
    WaiverCandidateRecordError,
    WaiverProposal,
    candidate_id,
    waiver_candidates_lock_path,
    waiver_candidates_path,
)
from booley.goals.apply_effects import FileEffect
from booley.mcp.goal_waivers import GoalWaiverService

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
POINT = "cp1:eyJwb2ludCI6MX0"
OTHER_POINT = "cp1:eyJwb2ludCI6Mn0"


def _binding(campaign: str = "camp-1", point_store: str = SHA_A) -> CampaignBinding:
    return CampaignBinding(
        campaign_id=campaign,
        manifest_sha256=SHA_A,
        point_store_sha256=point_store,
        campaign_path=f"sim/1/core/{campaign}/coverage.json",
        target_identity="vlnv:acme:ip:core:1.0#sim_core",
        target_selector="sim_core",
    )


def _proposal(
    point: str = POINT, sha: str = SHA_A, justification: str = "never driven"
) -> WaiverProposal:
    return WaiverProposal(
        point_id=point,
        source="rtl/core.sv",
        source_sha256=sha,
        reason="unreachable",
        justification=justification,
        evidence_refs=("rtl/core.sv:12",),
    )


def _record(tickets: Path, *proposals: WaiverProposal, **kwargs: Any) -> store.RecordOutcome:
    kwargs.setdefault("binding", _binding())
    kwargs.setdefault("invocation_id", "inv-1")
    kwargs.setdefault("now", NOW)
    binding = kwargs.pop("binding")
    return store.record_proposals(tickets, binding, proposals or (_proposal(),), **kwargs)


def _rejection(point: str = POINT, sha: str = SHA_A) -> Rejection:
    return Rejection(
        target_identity=_binding().target_identity,
        point_id=point,
        source_sha256=sha,
        rejected_at="2026-09-30T13:00:00Z",
        rejected_by="Ada <ada@example.com>",
    )


def _only(record: CandidateRecord) -> store.WaiverCandidate:
    (candidate,) = record.candidates.values()
    return candidate


# Accumulation -------------------------------------------------------------------


def test_first_proposal_creates_the_record_lazily(tmp_path: Path) -> None:
    record = store.load(tmp_path)
    assert record == CandidateRecord()
    assert record.is_empty
    assert not waiver_candidates_path(tmp_path).exists()
    assert store.load(tmp_path / "another-record").is_empty
    outcome = _record(tmp_path)

    expected_id = (
        "wc-"
        + hashlib.sha256(f"{_binding().target_identity}|{POINT}|{SHA_A}".encode()).hexdigest()[:12]
    )
    assert outcome == store.RecordOutcome(1, 0, (expected_id,))
    candidate = _only(store.load(tmp_path))
    assert candidate.candidate_id == expected_id == candidate_id(*candidate.key, SHA_A)
    assert candidate.proposal_count == 1
    assert candidate.first_recorded_at == candidate.last_recorded_at == "2026-09-30T12:00:00Z"
    if os.name == "posix":
        assert waiver_candidates_path(tmp_path).stat().st_mode & 0o777 == 0o644


def test_same_campaign_bumps_count_and_replaces_justification(tmp_path: Path) -> None:
    _record(tmp_path)
    _record(tmp_path, _proposal(justification="tied low"), invocation_id="inv-2", now=LATER)

    candidate = _only(store.load(tmp_path))
    assert candidate.proposal_count == 2
    assert candidate.proposal.justification == "tied low"
    assert candidate.first_recorded_at == "2026-09-30T12:00:00Z"
    assert candidate.last_recorded_at == "2026-09-30T12:05:00Z"
    assert candidate.last_invocation_id == "inv-2"


def test_different_campaign_replaces_entry_and_resets_count(tmp_path: Path) -> None:
    _record(tmp_path)
    _record(tmp_path)
    newer = _binding("camp-2", point_store=SHA_B)
    _record(tmp_path, binding=newer, now=LATER)

    candidate = _only(store.load(tmp_path))
    assert candidate.binding == newer
    assert candidate.proposal_count == 1
    assert candidate.first_recorded_at == "2026-09-30T12:05:00Z"


def test_changed_source_on_same_key_replaces_entry(tmp_path: Path) -> None:
    _record(tmp_path)
    _record(tmp_path, _proposal(sha=SHA_B))

    candidate = _only(store.load(tmp_path))
    assert candidate.proposal.source_sha256 == SHA_B
    assert candidate.proposal_count == 1


def test_batch_naming_a_point_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="once"):
        _record(tmp_path, _proposal(), _proposal())
    assert not waiver_candidates_path(tmp_path).exists()


def test_naive_now_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _record(tmp_path, now=datetime(2026, 9, 30, 12, 0, 0))


# Rejections ---------------------------------------------------------------------


def test_rejected_proposal_is_filtered_and_never_counted(tmp_path: Path) -> None:
    record = store.rejected_record(store.load(tmp_path), [_rejection()])
    waiver_candidates_path(tmp_path).write_bytes(record.to_bytes())

    outcome = _record(tmp_path, _proposal(), _proposal(OTHER_POINT))

    assert (outcome.recorded, outcome.filtered_by_rejection) == (1, 1)
    record = store.load(tmp_path)
    assert list(record.candidates) == [(_binding().target_identity, OTHER_POINT)]
    rejected = store.rejected_record(record, [_rejection(OTHER_POINT)])
    assert not rejected.candidates
    assert (
        store.rejected_record(
            rejected, [replace(_rejection(), rejected_at="2027-01-01T00:00:00Z")]
        )
        == rejected
    )


def test_changed_source_reopens_a_rejected_point(tmp_path: Path) -> None:
    record = store.rejected_record(store.load(tmp_path), [_rejection(sha=SHA_A)])
    waiver_candidates_path(tmp_path).write_bytes(record.to_bytes())

    outcome = _record(tmp_path, _proposal(sha=SHA_B))

    assert (outcome.recorded, outcome.filtered_by_rejection) == (1, 0)
    assert _only(store.load(tmp_path)).proposal.source_sha256 == SHA_B


def test_rejection_of_another_source_keeps_the_candidate(tmp_path: Path) -> None:
    _record(tmp_path, _proposal(sha=SHA_B))
    record = store.rejected_record(store.load(tmp_path), [_rejection(sha=SHA_A)])
    waiver_candidates_path(tmp_path).write_bytes(record.to_bytes())
    assert len(store.load(tmp_path).candidates) == 1


# Encoding and fail-closed parsing ----------------------------------------------


def _stored(tmp_path: Path) -> dict[str, Any]:
    _record(tmp_path)
    record = store.rejected_record(store.load(tmp_path), [_rejection(OTHER_POINT)])
    waiver_candidates_path(tmp_path).write_bytes(record.to_bytes())
    return json.loads(waiver_candidates_path(tmp_path).read_text(encoding="utf-8"))


def test_encoding_is_canonical_and_round_trips(tmp_path: Path) -> None:
    document = _stored(tmp_path)
    raw = waiver_candidates_path(tmp_path).read_text(encoding="utf-8")

    assert raw == json.dumps(document, indent=2, sort_keys=True) + "\n"
    assert document["schema"] == 1 and "slug" not in document
    (entry,) = document["candidates"].values()
    assert entry["campaign_path"] == "sim/1/core/camp-1/coverage.json"
    assert entry["reason"] == "unreachable" and entry["proof_reference"] == ""
    assert store.load(tmp_path).to_bytes() == raw.encode()


def _candidate_entry(document: dict[str, Any]) -> dict[str, Any]:
    return next(iter(document["candidates"].values()))


_CORRUPTIONS: dict[str, Any] = {
    "schema-2": lambda d: d.update(schema=2),
    "schema-bool": lambda d: d.update(schema=True),
    "schema-missing": lambda d: d.pop("schema"),
    "extra-top-level": lambda d: d.update(extra=1),
    "candidates-list": lambda d: d.update(candidates=[]),
    "reason": lambda d: _candidate_entry(d).update(reason="maybe"),
    "count-zero": lambda d: _candidate_entry(d).update(proposal_count=0),
    "count-bool": lambda d: _candidate_entry(d).update(proposal_count=True),
    "timestamp": lambda d: _candidate_entry(d).update(last_recorded_at="2026-09-30 12:00"),
    "timestamp-order": lambda d: _candidate_entry(d).update(
        first_recorded_at="2027-01-01T00:00:00Z"
    ),
    "bad-sha": lambda d: _candidate_entry(d).update(manifest_sha256="sha256:XYZ"),
    "escaping-path": lambda d: _candidate_entry(d).update(campaign_path="../x.json"),
    "moved-id": lambda d: d.update(candidates={"wc-000000000000": _candidate_entry(d)}),
    "rejection-point": lambda d: d["rejections"][0].update(point_id="bad"),
    "candidate-id": lambda d: _candidate_entry(d).update(candidate_id="wc-000000000000"),
    "unknown-field": lambda d: _candidate_entry(d).update(note="hi"),
    "missing-field": lambda d: _candidate_entry(d).pop("justification"),
    "rejection-field": lambda d: d["rejections"][0].update(rejected_by=""),
    "duplicate-rejection": lambda d: d["rejections"].append(dict(d["rejections"][0])),
}


@pytest.mark.parametrize("name", sorted(_CORRUPTIONS))
def test_invalid_record_fails_closed(tmp_path: Path, name: str) -> None:
    document = _stored(tmp_path)
    _CORRUPTIONS[name](document)
    path = waiver_candidates_path(tmp_path)
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()

    diagnostic = {"rejection-point": "point_id", "timestamp": "canonical UTC"}.get(
        name, "is invalid"
    )
    with pytest.raises(WaiverCandidateRecordError, match=diagnostic):
        store.load(tmp_path)
    with pytest.raises(WaiverCandidateRecordError):
        _record(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"\xff\xfe", b'{"schema": 1, "schema": 1}', b""],
    ids=["syntax", "not-utf8", "duplicate-key", "empty"],
)
def test_undecodable_record_fails_closed(tmp_path: Path, raw: bytes) -> None:
    path = waiver_candidates_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(WaiverCandidateRecordError, match="not valid JSON"):
        store.load(tmp_path)


# Locking and crash safety ------------------------------------------------------


def test_lock_wait_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "LOCK_TIMEOUT_SECONDS", 0.2)
    lock = waiver_candidates_lock_path(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle)
        try:
            with pytest.raises(WaiverCandidateLockError, match="stayed locked"):
                _record(tmp_path)
        finally:
            release_file_lock(handle)
    assert not waiver_candidates_path(tmp_path).exists()
    assert _record(tmp_path).recorded == 1


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_crash_mid_publish_keeps_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _record(tmp_path)
    path = waiver_candidates_path(tmp_path)
    before = path.read_bytes()

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash")

    with monkeypatch.context() as patch:
        if failing == "fsync":
            patch.setattr(os, "fsync", crash)
        else:
            patch.setattr(Path, "replace", crash)
        with pytest.raises(OSError, match="simulated crash"):
            _record(tmp_path, _proposal(OTHER_POINT))

    assert path.read_bytes() == before
    assert {entry.name for entry in path.parent.iterdir()} == {
        path.name,
        waiver_candidates_lock_path(tmp_path).name,
    }
    assert _record(tmp_path, _proposal(OTHER_POINT)).recorded == 1


_WRITER = """
import sys
from datetime import UTC, datetime
from pathlib import Path
from booley.flows.sim import waiver_candidates as store

directory, name, rounds, shared = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
binding = store.CampaignBinding(
    "camp-1", "sha256:" + "a" * 64, "sha256:" + "a" * 64,
    "sim/1/core/camp-1/coverage.json", "vlnv:acme:ip:core:1.0#sim_core", "sim_core",
)
for index in range(rounds):
    own = f"cp1:{name}{index}"
    proposals = [
        store.WaiverProposal(point, "rtl/core.sv", "sha256:" + "a" * 64, "excluded", "x")
        for point in (shared, own)
    ]
    store.record_proposals(
        directory, binding, proposals,
        invocation_id=f"{name}-{index}", now=datetime.now(UTC),
    )
"""


def test_concurrent_writers_never_lose_updates(tmp_path: Path) -> None:
    rounds = 25
    env = {**os.environ, "PYTHONPATH": str(Path(booley.__file__).parents[1])}
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(tmp_path), name, str(rounds), POINT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("A", "B")
    ]
    for writer in writers:
        _, stderr = writer.communicate(timeout=120)
        assert writer.returncode == 0, stderr.decode()

    record = store.load(tmp_path)
    target = _binding().target_identity
    assert record.candidates[(target, POINT)].proposal_count == 2 * rounds
    own = {key[1] for key in record.candidates} - {POINT}
    assert own == {f"cp1:{name}{index}" for name in "AB" for index in range(rounds)}


@pytest.fixture(autouse=True)
def isolated_owner(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    reset_cache()


def test_internal_record_construction_does_not_validate_fields() -> None:
    with patch.object(store, "_point_id", wraps=store._point_id) as validate:
        binding, proposal, rejection = _binding(), _proposal(), _rejection()
        candidate = store.WaiverCandidate(
            binding, proposal, 1, "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z", "inv"
        )
        assert validate.call_count == 0, "trusted construction repeated field validation"
        record = CandidateRecord(candidates={candidate.key: replace(candidate, proposal_count=2)})
        store.rejected_record(record, [rejection])
        assert validate.call_count == 0, "trusted construction repeated field validation"


def test_proposal_ingress_validates_once(tmp_path: Path) -> None:
    binding, proposal = _binding(), _proposal()
    with patch.object(store, "_point_id", wraps=store._point_id) as validate:
        store.record_proposals(tmp_path, binding, [proposal], invocation_id="inv", now=NOW)
        assert validate.call_count == 1


@pytest.mark.parametrize("binding,proposal", [("bad", None), (None, "bad")])
def test_proposal_ingress_rejects_invalid_reason_and_binding(tmp_path, binding, proposal):
    binding = replace(_binding(), campaign_path="../bad") if binding else _binding()
    proposal = replace(_proposal(), reason="bad") if proposal else _proposal()
    with pytest.raises(BoundaryError):
        store.record_proposals(tmp_path, binding, [proposal], invocation_id="inv", now=NOW)
    assert not waiver_candidates_path(tmp_path).exists()


@pytest.mark.parametrize("legacy", ["matching", "ignored", "absent"])
def test_legacy_record_is_read_and_new_serialization_omits_slug(tmp_path, legacy):
    record = SimpleNamespace(id="evidence-20260930T120000Z", worktree_path=str(tmp_path))
    service = GoalWaiverService(tmp_path, record)
    path = service.paths.root / "waiver-candidates.json"
    document = {"schema": 1, "candidates": {}, "rejections": []}
    if legacy != "absent":
        document["slug"] = record.id if legacy == "matching" else {"legacy": "ignored"}
    replay = FileEffect(path, None, json.dumps(document).encode(), "candidate")
    replay.apply()
    replay.apply()
    item = SimpleNamespace(
        target_identity=_binding().target_identity, point_id=POINT, source_sha256=SHA_A
    )
    proposal = SimpleNamespace(waiver=SimpleNamespace(candidate=item))
    decision = SimpleNamespace(at="2026-09-30T13:00:00Z", session_key="Ada")
    try:
        (effect,) = service.rejection(proposal, decision)
    except WaiverCandidateRecordError as exc:
        pytest.fail(f"legacy record was not accepted: {exc}")
    effect.apply()
    written = json.loads(path.read_bytes())
    assert "slug" not in written, "new record must omit slug"
    assert written["rejections"][0]["point_id"] == POINT
    assert service.rejection(proposal, decision)[0].after == effect.after
