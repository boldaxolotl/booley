"""Actionable Ticket migration warnings, with stable per-path waiver identities."""

import hashlib
from pathlib import Path

import pytest

from booley.harness import doctor


def _findings(project: Path):
    reporter = doctor._Reporter.create()
    doctor._check_ticket_leftovers(project, reporter.warn_)
    return reporter.findings


def test_empty_board_directories_and_ticket_history_are_silent(tmp_path):
    for name in ("board", "state", "logs", "locks", "waiver-candidates", "history"):
        (tmp_path / "tickets" / name).mkdir(parents=True)
    (tmp_path / "tickets/history/preserved.md").write_text("inert history")
    assert _findings(tmp_path) == []


def test_each_nonempty_directory_has_a_waivable_warning_and_delete_fix(tmp_path):
    for name in ("board", "state", "logs", "locks", "waiver-candidates"):
        path = tmp_path / "tickets" / name
        path.mkdir(parents=True)
        if name != "locks":
            (path / "leftover").write_text("data")
    findings = _findings(tmp_path)
    assert {f.subject for f in findings} == {"board", "state", "logs", "waiver-candidates"}
    assert {f.check_id for f in findings} == {"tickets.leftover-board"}
    assert all(f.severity == "warn" for f in findings)
    for finding in findings:
        assert finding.fix == f"delete {tmp_path / 'tickets' / finding.subject}"
        assert "empty directories: locks" in finding.message


@pytest.mark.parametrize("name", ["ticket_creation.md", "ticket_defaults.md"])
@pytest.mark.parametrize("shipped", [False, True])
def test_guidance_warns_to_delete_template_or_move_edited_rules(
    tmp_path, monkeypatch, name, shipped
):
    content = b"# Project rules\nAlways run verification.\n"
    if shipped:
        monkeypatch.setattr(
            doctor, "_SHIPPED_TICKET_GUIDANCE_HASHES", {hashlib.sha256(content).hexdigest()}
        )
    (tmp_path / name).write_bytes(content.replace(b"\n", b"\r\n"))
    (finding,) = _findings(tmp_path)
    assert finding.check_id == "tickets.leftover-guidance"
    assert finding.subject == name
    assert finding.fix.endswith(f"delete {tmp_path / name}")
    assert (
        "move its rules into a Goalset under .booley_project/goalsets/" in finding.fix
    ) is not shipped
