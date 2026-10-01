"""Done findings complete verification but require unaccepted human review."""

from __future__ import annotations

import json

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board.acceptance_ledger import read_acceptance
from booley.ticket_board.operations import op_handoff
from booley.ticket_board.paths import runtime_file
from tests.ticket_board.test_provisional_handoff import _HEADS, _set_destination_done
from tests.ticket_board.test_ticket_board import (
    _make_handoff_ready_ticket,
    _synthetic_non_git_ticket_view,  # noqa: F401 — synthetic Ticket baseline fixture
    make_tio,
)


@pytest.fixture
def tio(tmp_path, monkeypatch):
    tio = make_tio(tmp_path)
    _make_handoff_ready_ticket(tio, "t1")
    monkeypatch.setattr("booley.ticket_board.operations._handoff_basis_heads", lambda *_a: _HEADS)
    monkeypatch.setattr(
        "booley.ticket_board.operations._handoff_ticket_identity", lambda *_a: None
    )
    path = runtime_file(tio.logs_dir, "t1", "booley_state.json")
    state = DevelopmentState.load(path)
    state.init_criteria({"sim_pass": True, "review_rtl_bugs_done": False})
    state.set_criterion("sim_pass", True)
    state.set_criterion(
        "review_rtl_bugs_done",
        True,
        detail={
            "issue_list": [
                {
                    "finding_id": "minor",
                    "severity": "MINOR",
                    "disposition": "current",
                    "status": "current",
                }
            ]
        },
    )
    state.set_criterion("_report_submitted", True)
    state.save()
    return tio


@pytest.mark.parametrize("done", [False, True])
def test_current_done_findings_override_success_without_freezing(tio, done, monkeypatch):
    if done:
        _set_destination_done(tio, "t1")
    monkeypatch.setattr(
        "booley.ticket_board.operations.op_complete", lambda *_a: pytest.fail("auto complete")
    )
    assert op_handoff(tio, "t1") is True
    assert tio.find_ticket("t1")["status"] == "review"
    assert read_acceptance(tio.logs_dir / "t1").kind == "unavailable"
    marker = json.loads((tio.logs_dir / "t1" / "review" / "advisory-handoff.json").read_text())
    assert marker["heads"] == _HEADS
    assert marker["findings"][0]["finding_id"] == "minor"


def test_advisory_retry_preserves_marker_and_does_not_accept(tio):
    assert op_handoff(tio, "t1")
    path = tio.logs_dir / "t1" / "review" / "advisory-handoff.json"
    before = path.read_bytes()
    assert op_handoff(tio, "t1")
    assert path.read_bytes() == before
    assert read_acceptance(tio.logs_dir / "t1").kind == "unavailable"


@pytest.mark.parametrize("field", ["execution_id", "heads", "schema"])
def test_corrupt_or_foreign_marker_refuses_review(tio, field):
    from booley.ticket_board.advisory_handoff import advisory_marker_current

    assert op_handoff(tio, "t1")
    path = tio.logs_dir / "t1" / "review" / "advisory-handoff.json"
    marker = json.loads(path.read_text())
    marker[field] = {"outer": "b" * 40} if field == "heads" else "foreign"
    path.write_text(json.dumps(marker))
    with pytest.raises(ValueError):
        advisory_marker_current(tio, "t1")


def test_handoff_waits_for_jobs(tio, monkeypatch):
    monkeypatch.setattr("booley.ticket_board.operations._handoff_jobs_clear", lambda *_a: False)
    assert op_handoff(tio, "t1") is False
    assert tio.find_ticket("t1")["status"] == "running"
    assert not (tio.logs_dir / "t1" / "review" / "advisory-handoff.json").exists()


def test_handoff_requires_strict_mandatory_acceptance(tio):
    path = runtime_file(tio.logs_dir, "t1", "booley_state.json")
    state = DevelopmentState.load(path)
    state.set_criterion("sim_pass", False)
    state.save()
    assert op_handoff(tio, "t1") is False
    assert tio.find_ticket("t1")["status"] == "running"


def test_stranded_advisory_handoff_can_request_inspection(tio):
    from booley.ticket_board.review_lifecycle import (
        _awaits_provisional_inspection,
        _validate_action,
    )

    assert op_handoff(tio, "t1")
    assert _awaits_provisional_inspection(tio, "t1", None)
    _validate_action(tio, "t1", "request", False)
    assert not _awaits_provisional_inspection(tio, "t1", {"entry": "published"})


def test_prior_acceptance_is_retained_and_requires_human_recovery(tio, monkeypatch, capsys):
    from booley.ticket_board.acceptance_ledger import freeze_acceptance

    state = DevelopmentState.load(runtime_file(tio.logs_dir, "t1", "booley_state.json"))
    freeze_acceptance(
        tio.logs_dir / "t1",
        state,
        execution_id="prior",
        ticket_identity=None,
        participant_heads=_HEADS,
    )
    monkeypatch.setattr(
        "booley.ticket_board.operations._bind_existing_handoff_snapshot", lambda *_a: True
    )
    before = read_acceptance(tio.logs_dir / "t1").snapshot
    assert op_handoff(tio, "t1") is False
    assert read_acceptance(tio.logs_dir / "t1").snapshot == before
    assert "board approve" in capsys.readouterr().err
    assert tio.find_ticket("t1")["status"] == "running"
