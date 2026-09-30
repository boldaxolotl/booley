"""Tests for the per-Ticket Waiver Candidate store (ADR 0066)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import booley
from booley.core.boundary import BoundaryError
from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.board_layout import (
    waiver_candidates_lock_path,
    waiver_candidates_path,
    waiver_candidates_root,
)
from booley.ticket_board.waiver_candidates import (
    CampaignBinding,
    CandidateRecord,
    Rejection,
    WaiverCandidateLockError,
    WaiverCandidateRecordError,
    WaiverProposal,
    candidate_id,
)

SLUG = "t1"
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
    return store.record_proposals(tickets, SLUG, binding, proposals or (_proposal(),), **kwargs)


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


def test_missing_record_is_empty(tmp_path: Path) -> None:
    record = store.load(tmp_path, SLUG)
    assert record == CandidateRecord(SLUG)
    assert record.is_empty
    assert not waiver_candidates_root(tmp_path).exists()


def test_first_proposal_creates_the_record_lazily(tmp_path: Path) -> None:
    outcome = _record(tmp_path)

    expected_id = (
        "wc-"
        + hashlib.sha256(f"{_binding().target_identity}|{POINT}|{SHA_A}".encode()).hexdigest()[:12]
    )
    assert outcome == store.RecordOutcome(1, 0, (expected_id,))
    candidate = _only(store.load(tmp_path, SLUG))
    assert candidate.candidate_id == expected_id == candidate_id(*candidate.key, SHA_A)
    assert candidate.proposal_count == 1
    assert candidate.first_recorded_at == candidate.last_recorded_at == "2026-09-30T12:00:00Z"
    if os.name == "posix":
        assert waiver_candidates_path(tmp_path, SLUG).stat().st_mode & 0o777 == 0o644


def test_same_campaign_bumps_count_and_replaces_justification(tmp_path: Path) -> None:
    _record(tmp_path)
    _record(tmp_path, _proposal(justification="tied low"), invocation_id="inv-2", now=LATER)

    candidate = _only(store.load(tmp_path, SLUG))
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

    candidate = _only(store.load(tmp_path, SLUG))
    assert candidate.binding == newer
    assert candidate.proposal_count == 1
    assert candidate.first_recorded_at == "2026-09-30T12:05:00Z"


def test_changed_source_on_same_key_replaces_entry(tmp_path: Path) -> None:
    _record(tmp_path)
    _record(tmp_path, _proposal(sha=SHA_B))

    candidate = _only(store.load(tmp_path, SLUG))
    assert candidate.proposal.source_sha256 == SHA_B
    assert candidate.proposal_count == 1


def test_batch_naming_a_point_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="once"):
        _record(tmp_path, _proposal(), _proposal())
    assert not waiver_candidates_path(tmp_path, SLUG).exists()


def test_naive_now_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _record(tmp_path, now=datetime(2026, 9, 30, 12, 0, 0))


@pytest.mark.parametrize(
    "change",
    [
        {"point_id": "not-a-point"},
        {"source": "/abs/core.sv"},
        {"source": "rtl/../core.sv"},
        {"source_sha256": "sha256:short"},
        {"reason": "maybe"},
        {"justification": ""},
        {"evidence_refs": "rtl/core.sv:12"},
        {"evidence_refs": ("",)},
    ],
)
def test_invalid_proposal_is_refused(change: dict[str, Any]) -> None:
    fields = {
        "point_id": POINT,
        "source": "rtl/core.sv",
        "source_sha256": SHA_A,
        "reason": "excluded",
        "justification": "x",
        **change,
    }
    with pytest.raises(BoundaryError):
        WaiverProposal(**fields)


# Rejections ---------------------------------------------------------------------


def test_rejected_proposal_is_filtered_and_never_counted(tmp_path: Path) -> None:
    store.record_rejections(tmp_path, SLUG, [_rejection()])

    outcome = _record(tmp_path, _proposal(), _proposal(OTHER_POINT))

    assert (outcome.recorded, outcome.filtered_by_rejection) == (1, 1)
    record = store.load(tmp_path, SLUG)
    assert list(record.candidates) == [(_binding().target_identity, OTHER_POINT)]


def test_changed_source_reopens_a_rejected_point(tmp_path: Path) -> None:
    store.record_rejections(tmp_path, SLUG, [_rejection(sha=SHA_A)])

    outcome = _record(tmp_path, _proposal(sha=SHA_B))

    assert (outcome.recorded, outcome.filtered_by_rejection) == (1, 0)
    assert _only(store.load(tmp_path, SLUG)).proposal.source_sha256 == SHA_B


def test_rejections_are_an_idempotent_set_and_drop_matching_candidates(tmp_path: Path) -> None:
    _record(tmp_path, _proposal(), _proposal(OTHER_POINT))

    assert store.record_rejections(tmp_path, SLUG, [_rejection()]) == 1
    before = waiver_candidates_path(tmp_path, SLUG).read_bytes()
    later = Rejection(**{**_rejection().to_json(), "rejected_at": "2026-10-01T00:00:00Z"})
    assert store.record_rejections(tmp_path, SLUG, [later]) == 0

    assert waiver_candidates_path(tmp_path, SLUG).read_bytes() == before
    record = store.load(tmp_path, SLUG)
    assert record.rejections == (_rejection(),)
    assert list(record.candidates) == [(_binding().target_identity, OTHER_POINT)]
    assert record.is_rejected(_binding().target_identity, POINT, SHA_A)
    assert not record.is_rejected(_binding().target_identity, POINT, SHA_B)


def test_rejection_of_another_source_keeps_the_candidate(tmp_path: Path) -> None:
    _record(tmp_path, _proposal(sha=SHA_B))
    store.record_rejections(tmp_path, SLUG, [_rejection(sha=SHA_A)])
    assert len(store.load(tmp_path, SLUG).candidates) == 1


# Clear and discard ------------------------------------------------------------


def test_clear_candidates_keeps_rejections(tmp_path: Path) -> None:
    _record(tmp_path, _proposal(OTHER_POINT))
    store.record_rejections(tmp_path, SLUG, [_rejection()])

    store.clear_candidates(tmp_path, SLUG)

    record = store.load(tmp_path, SLUG)
    assert record.candidates == {}
    assert record.rejections == (_rejection(),)


def test_clear_candidates_without_rejections_removes_the_file(tmp_path: Path) -> None:
    _record(tmp_path)
    store.clear_candidates(tmp_path, SLUG)
    assert not waiver_candidates_path(tmp_path, SLUG).exists()
    store.clear_candidates(tmp_path, SLUG)  # idempotent on an absent record


def test_discard_deletes_the_record_even_when_corrupt(tmp_path: Path) -> None:
    _record(tmp_path)
    store.record_rejections(tmp_path, SLUG, [_rejection(OTHER_POINT)])
    assert store.discard(tmp_path, SLUG) is True
    assert store.load(tmp_path, SLUG).is_empty

    path = waiver_candidates_path(tmp_path, SLUG)
    path.write_text("{not json", encoding="utf-8")
    assert store.discard(tmp_path, SLUG) is True
    assert store.discard(tmp_path, SLUG) is False


def test_records_are_per_ticket(tmp_path: Path) -> None:
    _record(tmp_path)
    assert store.load(tmp_path, "t2").is_empty


# Encoding and fail-closed parsing ----------------------------------------------


def _stored(tmp_path: Path) -> dict[str, Any]:
    _record(tmp_path)
    store.record_rejections(tmp_path, SLUG, [_rejection(OTHER_POINT)])
    return json.loads(waiver_candidates_path(tmp_path, SLUG).read_text(encoding="utf-8"))


def test_encoding_is_canonical_and_round_trips(tmp_path: Path) -> None:
    document = _stored(tmp_path)
    raw = waiver_candidates_path(tmp_path, SLUG).read_text(encoding="utf-8")

    assert raw == json.dumps(document, indent=2, sort_keys=True) + "\n"
    assert document["schema"] == 1 and document["slug"] == SLUG
    (entry,) = document["candidates"].values()
    assert entry["campaign_path"] == "sim/1/core/camp-1/coverage.json"
    assert entry["reason"] == "unreachable" and entry["proof_reference"] == ""
    assert store.load(tmp_path, SLUG).to_bytes() == raw.encode()


def _candidate_entry(document: dict[str, Any]) -> dict[str, Any]:
    return next(iter(document["candidates"].values()))


_CORRUPTIONS: dict[str, Any] = {
    "schema-2": lambda d: d.update(schema=2),
    "schema-bool": lambda d: d.update(schema=True),
    "schema-missing": lambda d: d.pop("schema"),
    "slug-mismatch": lambda d: d.update(slug="t2"),
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
    path = waiver_candidates_path(tmp_path, SLUG)
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(WaiverCandidateRecordError, match="is invalid"):
        store.load(tmp_path, SLUG)
    with pytest.raises(WaiverCandidateRecordError):
        _record(tmp_path)
    with pytest.raises(WaiverCandidateRecordError):
        store.clear_candidates(tmp_path, SLUG)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"\xff\xfe", b'{"schema": 1, "schema": 1}', b""],
    ids=["syntax", "not-utf8", "duplicate-key", "empty"],
)
def test_undecodable_record_fails_closed(tmp_path: Path, raw: bytes) -> None:
    path = waiver_candidates_path(tmp_path, SLUG)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(WaiverCandidateRecordError, match="not valid JSON"):
        store.load(tmp_path, SLUG)


def test_moved_candidate_under_another_id_fails_closed(tmp_path: Path) -> None:
    document = _stored(tmp_path)
    document["candidates"] = {"wc-000000000000": _candidate_entry(document)}
    waiver_candidates_path(tmp_path, SLUG).write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WaiverCandidateRecordError, match="does not match"):
        store.load(tmp_path, SLUG)


# Locking and crash safety ------------------------------------------------------


def test_lock_wait_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "LOCK_TIMEOUT_SECONDS", 0.2)
    lock = waiver_candidates_lock_path(tmp_path, SLUG)
    lock.parent.mkdir(parents=True)
    with lock.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle)
        try:
            with pytest.raises(WaiverCandidateLockError, match="stayed locked"):
                _record(tmp_path)
        finally:
            release_file_lock(handle)
    assert not waiver_candidates_path(tmp_path, SLUG).exists()
    assert _record(tmp_path).recorded == 1


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_crash_mid_publish_keeps_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _record(tmp_path)
    path = waiver_candidates_path(tmp_path, SLUG)
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
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]
    assert _record(tmp_path, _proposal(OTHER_POINT)).recorded == 1


_WRITER = """
import sys
from datetime import UTC, datetime
from pathlib import Path
from booley.ticket_board import waiver_candidates as store

tickets, name, rounds, shared = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
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
        tickets, "t1", binding, proposals,
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

    record = store.load(tmp_path, SLUG)
    target = _binding().target_identity
    assert record.candidates[(target, POINT)].proposal_count == 2 * rounds
    own = {key[1] for key in record.candidates} - {POINT}
    assert own == {f"cp1:{name}{index}" for name in "AB" for index in range(rounds)}
