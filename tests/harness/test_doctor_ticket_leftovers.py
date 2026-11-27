"""Actionable Ticket migration warnings, with stable per-path waiver identities."""

import argparse
import json
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.harness import doctor
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.test_doctor import _write_project

_GUIDANCE = Path(__file__).resolve().parents[1] / "fixtures/ticket_guidance"
_PROVENANCE = json.loads((_GUIDANCE / "provenance.json").read_text(encoding="utf-8"))
_NAMES = ("ticket_creation.md", "ticket_defaults.md")


def _project(root: Path, monkeypatch) -> Path:
    """A healthy Project (empty Ticket Board directories) ready for a whole Doctor run."""
    booley_dir = _write_project(root)
    patch_doctor_environment(monkeypatch, root, booley_dir)
    return booley_dir


def _leftovers(root: Path):
    """The findings a whole Doctor run reports for Ticket leftovers."""
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), root)
    return [f for f in result.findings if (f.check_id or "").startswith("tickets.")]


def test_each_nonempty_directory_has_a_waivable_warning_and_delete_fix(tmp_path, monkeypatch):
    tickets = _project(tmp_path, monkeypatch) / "tickets"
    for name in ("board", "state", "logs", "waiver-candidates"):
        (tickets / name).mkdir(exist_ok=True)
        (tickets / name / "leftover").write_text("data")
    # Ticket History is inert, even when it holds files.
    (tickets / "history").mkdir()
    (tickets / "history" / "preserved.md").write_text("inert history")

    findings = _leftovers(tmp_path)

    assert {f.subject for f in findings} == {"board", "state", "logs", "waiver-candidates"}
    assert {f.check_id for f in findings} == {"tickets.leftover-board"}
    assert all(f.severity is Severity.WARN for f in findings)
    for finding in findings:
        assert finding.fix == (
            f"archive any Ticket you still need, then delete {tickets / finding.subject}"
        )
        assert "empty directories: locks" in finding.message


@pytest.mark.parametrize("digest", [*sorted(_PROVENANCE), "edited"])
def test_doctor_reports_leftover_ticket_guidance(tmp_path, monkeypatch, digest):
    booley_dir = _project(tmp_path, monkeypatch)
    edited = digest == "edited"
    source = next(iter(_PROVENANCE.values())) if edited else _PROVENANCE[digest]
    content = (_GUIDANCE / source["fixture"]).read_bytes().replace(b"\r\n", b"\n")
    if edited:
        content += b"\nProject rule: preserve my design.\n"
    for name in _NAMES:
        (booley_dir / name).write_bytes(content.replace(b"\n", b"\r\n"))

    findings = _leftovers(tmp_path)

    assert {f.check_id for f in findings} == {"tickets.leftover-guidance"}
    assert {f.subject: f.fix for f in findings} == {
        name: (
            f"move its rules into the Project's AGENTS.md, then delete {booley_dir / name}"
            if edited
            else f"delete {booley_dir / name}"
        )
        for name in _NAMES
    }


@pytest.mark.parametrize("kind", ["board", "guidance"])
def test_unreadable_leftovers_warn_without_crashing(tmp_path, monkeypatch, kind):
    booley_dir = _project(tmp_path, monkeypatch)
    if kind == "board":
        path = booley_dir / "tickets/board"
        method = "iterdir"
    else:
        path = booley_dir / "ticket_defaults.md"
        path.write_text("Project rules")
        method = "read_bytes"
    original = getattr(Path, method)

    def unreadable(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("access denied")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, method, unreadable)

    (finding,) = _leftovers(tmp_path)

    assert finding.severity is Severity.WARN
    assert finding.check_id == (
        "tickets.leftover-board" if kind == "board" else "tickets.leftover-guidance"
    )
    assert "access denied" in finding.message
    assert "delet" in finding.fix
    assert path.exists()
