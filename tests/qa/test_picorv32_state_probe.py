"""Run-owned Grant and Session recovery gates do not touch borrowed state."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from qa.scenarios.picorv32.state_probe import TransitionError, denied, grant_replacement, ready


def _snapshot(grant, grant_epoch, session_epoch, valid, running, mount):
    return {
        "project_root": "/qa-owned/project",
        "eda_kind": "vivado",
        "grant_registration": grant,
        "grant_epoch": grant_epoch,
        "session_grant_epoch": session_epoch,
        "session_valid": valid,
        "session_running": running,
        "mount_probe": mount,
    }


def _transition(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    denial_log = evidence / "fpga-while-revoked.log"
    denial_log.write_text(
        'argv: ["booley", "flow", "fpga"]\nexit: 2\n'
        "ERROR: refusing Sandbox startup: host-issued spec stamp is missing or corrupt\n"
    )
    before = _snapshot("old-owned", 1, 1, True, True, True)
    revoked = _snapshot(None, 2, 1, False, False, False)
    regranted = _snapshot("new-owned", 3, 1, False, False, False)
    issued = _snapshot("new-owned", 3, 3, True, False, False)
    started = _snapshot("new-owned", 3, 3, True, True, True)
    borrowed = {
        "grants": [{"project": "/borrowed/project", "registration": "shared"}],
        "installations": [{"registration": "shared", "source": "/tools/vivado"}],
    }
    return {
        "owner": {
            "project_root": "/qa-owned/project",
            "registration": "new-owned",
            "run_owned_registrations": ["old-owned", "new-owned"],
            "evidence_root": str(evidence),
        },
        "states": {
            "before": before,
            "revoked": revoked,
            "regranted": regranted,
            "issued": issued,
            "started": started,
            "current": started,
        },
        "protected": {"before": borrowed, "after": copy.deepcopy(borrowed)},
        "denial_evidence": {
            "declared_expected": "denied",
            "flow_executed": False,
            "log_path": str(denial_log),
            "log_sha256": hashlib.sha256(denial_log.read_bytes()).hexdigest(),
        },
    }


def test_existing_grant_replaced_and_reissued_session_restores_mount(tmp_path):
    record = _transition(tmp_path)
    assert grant_replacement(record)["ready"]
    assert ready(record)["ready"]
    assert denied(record)["denial_pass"]


def test_regrant_without_seed_is_detected_before_doctor_or_fpga(tmp_path):
    record = _transition(tmp_path)
    record["states"]["current"] = record["states"]["regranted"]
    with pytest.raises(TransitionError, match="stale Session spec"):
        ready(record)
    record["states"]["issued"]["session_grant_epoch"] = 1
    with pytest.raises(TransitionError, match="stale Session spec"):
        grant_replacement(record)


def test_replacement_requires_distinct_grant_epochs_and_invalidated_session(tmp_path):
    record = _transition(tmp_path)
    for snapshot in record["states"].values():
        snapshot["grant_epoch"] = 1
    with pytest.raises(TransitionError, match="revoke then regrant"):
        grant_replacement(record)
    record = _transition(tmp_path / "validity")
    record["states"]["revoked"]["session_valid"] = True
    with pytest.raises(TransitionError, match="not invalidated"):
        grant_replacement(record)


def test_borrowed_grant_cannot_be_replaced(tmp_path):
    record = _transition(tmp_path)
    record["states"]["before"]["grant_registration"] = "borrowed"
    with pytest.raises(TransitionError, match="borrowed"):
        grant_replacement(record)


def test_replacement_registration_must_be_run_owned(tmp_path):
    record = _transition(tmp_path)
    record["owner"]["run_owned_registrations"].remove("new-owned")
    with pytest.raises(TransitionError, match="not in the run-owned ledger"):
        grant_replacement(record)


def test_negative_denial_is_independent_of_later_recovery(tmp_path):
    record = _transition(tmp_path)
    record["states"].pop("issued")
    record["states"].pop("started")
    assert denied(record)["denial_pass"]
    wrong = copy.deepcopy(record)
    wrong["denial_evidence"]["log_sha256"] = "0" * 64
    with pytest.raises(TransitionError, match="digest"):
        denied(wrong)


def test_unrelated_project_identity_rejected(tmp_path):
    record = _transition(tmp_path)
    record["states"]["current"]["project_root"] = "/borrowed/project"
    with pytest.raises(TransitionError, match="differs from run-owned"):
        ready(record)


def test_missing_epochs_do_not_compare_equal(tmp_path):
    record = _transition(tmp_path)
    record["states"]["issued"].pop("grant_epoch")
    record["states"]["issued"].pop("session_grant_epoch")
    with pytest.raises(TransitionError, match="integer epoch"):
        grant_replacement(record)


def test_denial_requires_exit_two_and_authority_identity(tmp_path):
    record = _transition(tmp_path)
    log_path = Path(record["denial_evidence"]["log_path"])
    log_path.write_text('argv: ["booley", "flow", "fpga"]\nexit: 1\nERROR: syntax error\n')
    record["denial_evidence"]["log_sha256"] = hashlib.sha256(log_path.read_bytes()).hexdigest()
    with pytest.raises(TransitionError, match="exit 2"):
        denied(record)
    log_path.write_text('argv: ["booley", "flow", "fpga"]\nexit: 2\nERROR: syntax error\n')
    record["denial_evidence"]["log_sha256"] = hashlib.sha256(log_path.read_bytes()).hexdigest()
    with pytest.raises(TransitionError, match="authority denial"):
        denied(record)


def test_borrowed_grant_and_installation_must_remain_unchanged(tmp_path):
    record = _transition(tmp_path)
    record["protected"]["after"]["grants"][0]["registration"] = "changed"
    with pytest.raises(TransitionError, match="borrowed grants changed"):
        grant_replacement(record)
    record = _transition(tmp_path / "second")
    record["protected"]["after"]["installations"].clear()
    with pytest.raises(TransitionError, match="borrowed installations changed"):
        ready(record)


def test_state_probe_cli_rejects_non_mapping_json_without_traceback(tmp_path):
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps([]))
    script = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/state_probe.py"
    result = subprocess.run(
        ["python3", str(script), str(snapshot), "ready"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "must be a JSON object" in result.stderr
    assert "Traceback" not in result.stderr
