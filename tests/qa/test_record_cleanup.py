"""Cleanup ledger publication validates the complete candidate before replacement."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.qa.test_triage import write_run

from qa import record_cleanup, triage


def candidate(run_id="run-1", resources=None):
    return {
        "run_record_format_version": 2,
        "record_type": "cleanup-ledger",
        "run_id": run_id,
        "resources": resources or [],
    }


def source(tmp_path, value, name="candidate.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path


def assert_unchanged(path, before):
    assert path.read_bytes() == before
    assert list(path.parent.glob(".cleanup-ledger.json-*")) == []


def test_complete_candidate_is_canonicalized_and_replaces_ledger(tmp_path):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    value = candidate(
        resources=[
            {
                "identity": "scratch",
                "actual_disposition": None,
                "active_authority_possible": False,
                "safe_shutdown_evidence_refs": ["evidence/log.txt"],
                "cleanup_reason": "  ",
            }
        ]
    )

    record_cleanup.replace_cleanup_ledger(root, source(tmp_path, value))

    written = triage.read_json(root / "cleanup-ledger.json")
    assert "cleanup_reason" not in written["resources"][0]
    assert written["resources"][0]["identity"] == "scratch"


@pytest.mark.parametrize("identity", ["", "  ", {"kind": "branch", "name": "owned"}])
def test_invalid_identity_preserves_original_bytes(tmp_path, identity):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()

    with pytest.raises(triage.TriageError, match="identity"):
        record_cleanup.replace_cleanup_ledger(
            root, source(tmp_path, candidate(resources=[{"identity": identity}]))
        )
    assert_unchanged(ledger, before)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"run_id": "other"}, "different run"),
        ({"record_type": "wrong"}, "cleanup-ledger"),
        ({"extra": True}, "Additional properties"),
    ],
)
def test_invalid_document_preserves_original_bytes(tmp_path, change, message):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()
    value = candidate()
    value.update(change)

    with pytest.raises(triage.TriageError, match=message):
        record_cleanup.replace_cleanup_ledger(root, source(tmp_path, value))
    assert_unchanged(ledger, before)


@pytest.mark.parametrize("field", ["updated_at", "private_note"])
def test_unknown_resource_field_preserves_original_bytes(tmp_path, field):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()

    with pytest.raises(triage.TriageError, match="Additional properties"):
        record_cleanup.replace_cleanup_ledger(
            root,
            source(tmp_path, candidate(resources=[{"identity": "scratch", field: "value"}])),
        )
    assert_unchanged(ledger, before)


def test_sealed_run_preserves_original_bytes(tmp_path):
    root = write_run(tmp_path, "run-1", "required", [])
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()

    with pytest.raises(triage.TriageError, match="sealed Scenario Run"):
        record_cleanup.replace_cleanup_ledger(root, source(tmp_path, candidate()))
    assert_unchanged(ledger, before)


def test_failed_atomic_publication_preserves_original_bytes(tmp_path, monkeypatch):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()

    def fail_fsync(_descriptor):
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated write failure"):
        record_cleanup.replace_cleanup_ledger(root, source(tmp_path, candidate()))
    assert_unchanged(ledger, before)


def test_failed_atomic_replace_preserves_original_bytes(tmp_path, monkeypatch):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()
    original_replace = Path.replace

    def fail_replace(path, target):
        if target == ledger:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        record_cleanup.replace_cleanup_ledger(root, source(tmp_path, candidate()))
    assert_unchanged(ledger, before)


def test_invalid_shutdown_evidence_preserves_original_bytes(tmp_path):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    ledger = root / "cleanup-ledger.json"
    before = ledger.read_bytes()
    value = candidate(
        resources=[
            {
                "identity": "scratch",
                "safe_shutdown_evidence_refs": ["evidence/missing.txt"],
            }
        ]
    )

    with pytest.raises(triage.TriageError, match="evidence"):
        record_cleanup.replace_cleanup_ledger(root, source(tmp_path, value))
    assert_unchanged(ledger, before)


def test_concurrent_publications_are_serialized(tmp_path, monkeypatch):
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    entered = threading.Event()
    release = threading.Event()
    original = triage.atomic_json
    calls = 0

    def controlled_write(path, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=5)
        original(path, value)

    monkeypatch.setattr(triage, "atomic_json", controlled_write)
    first = source(tmp_path, candidate(resources=[{"identity": "first"}]), "first.json")
    second = source(tmp_path, candidate(resources=[{"identity": "second"}]), "second.json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_first = pool.submit(record_cleanup.replace_cleanup_ledger, root, first)
        assert entered.wait(timeout=5)
        pending_second = pool.submit(record_cleanup.replace_cleanup_ledger, root, second)
        assert not pending_second.done()
        release.set()
        pending_first.result(timeout=5)
        pending_second.result(timeout=5)

    assert triage.read_json(root / "cleanup-ledger.json")["resources"] == [{"identity": "second"}]
