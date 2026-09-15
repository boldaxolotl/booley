"""Run-owned Grant and Session recovery gates do not touch borrowed state."""

import copy

import pytest
from qa.scenarios.picorv32.state_probe import TransitionError, denied, grant_replacement, ready


def _snapshot(grant, grant_epoch, session_epoch, valid, running, mount):
    return {"project_root": "/qa-owned/project", "eda_kind": "vivado",
            "grant_registration": grant, "grant_epoch": grant_epoch,
            "session_grant_epoch": session_epoch, "session_valid": valid,
            "session_running": running, "mount_probe": mount}


def _transition():
    before = _snapshot("old-owned", 1, 1, True, True, True)
    revoked = _snapshot(None, 2, 1, False, False, False)
    regranted = _snapshot("new-owned", 3, 1, False, False, False)
    issued = _snapshot("new-owned", 3, 3, True, False, False)
    started = _snapshot("new-owned", 3, 3, True, True, True)
    return {"owner": {"project_root": "/qa-owned/project", "registration": "new-owned",
                      "run_owned_registrations": ["old-owned", "new-owned"]},
            "states": {"before": before, "revoked": revoked, "regranted": regranted,
                       "issued": issued, "started": started, "current": started},
            "denial_evidence": {"declared_expected": "denied", "observed": "denied",
                                "flow_executed": False}}


def test_existing_grant_replaced_and_reissued_session_restores_mount():
    record = _transition()
    assert grant_replacement(record)["ready"]
    assert ready(record)["ready"]
    assert denied(record)["denial_pass"]


def test_regrant_without_seed_is_detected_before_doctor_or_fpga():
    record = _transition()
    record["states"]["current"] = record["states"]["regranted"]
    with pytest.raises(TransitionError, match="stale Session spec"):
        ready(record)
    record["states"]["issued"]["session_grant_epoch"] = 1
    with pytest.raises(TransitionError, match="stale Session spec"):
        grant_replacement(record)


def test_borrowed_grant_cannot_be_replaced():
    record = _transition()
    record["states"]["before"]["grant_registration"] = "borrowed"
    with pytest.raises(TransitionError, match="borrowed"):
        grant_replacement(record)


def test_negative_denial_is_independent_of_later_recovery():
    record = _transition()
    record["states"].pop("issued")
    record["states"].pop("started")
    assert denied(record)["denial_pass"]
    wrong = copy.deepcopy(record)
    wrong["denial_evidence"]["observed"] = "pass"
    with pytest.raises(TransitionError, match="not independently observed"):
        denied(wrong)


def test_unrelated_project_identity_rejected():
    record = _transition()
    record["states"]["current"]["project_root"] = "/borrowed/project"
    with pytest.raises(TransitionError, match="differs from run-owned"):
        ready(record)
