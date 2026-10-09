"""Actionable Ticket migration warnings, with stable per-path waiver identities."""

import hashlib
import json
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


_GUIDANCE = Path(__file__).resolve().parents[1] / "fixtures/ticket_guidance"
_PROVENANCE = json.loads((_GUIDANCE / "provenance.json").read_text())


def test_shipped_hash_set_matches_all_pinned_template_versions():
    assert set(_PROVENANCE) == doctor._SHIPPED_TICKET_GUIDANCE_HASHES
    for digest, source in _PROVENANCE.items():
        content = (_GUIDANCE / source["fixture"]).read_bytes()
        assert hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest() == digest
        assert len(source["commit"]) == 40
        assert source["path"].endswith("_TEMPLATE.md")


@pytest.mark.parametrize("name", ["ticket_creation.md", "ticket_defaults.md"])
@pytest.mark.parametrize("digest", sorted(_PROVENANCE))
def test_shipped_guidance_warns_to_delete_real_template(tmp_path, name, digest):
    content = (_GUIDANCE / _PROVENANCE[digest]["fixture"]).read_bytes()
    (tmp_path / name).write_bytes(content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    (finding,) = _findings(tmp_path)
    assert finding.check_id == "tickets.leftover-guidance"
    assert finding.subject == name
    assert finding.fix == f"delete {tmp_path / name}"


@pytest.mark.parametrize("name", ["ticket_creation.md", "ticket_defaults.md"])
def test_edited_guidance_warns_to_move_rules_before_delete(tmp_path, name):
    source = next(iter(_PROVENANCE.values()))
    (tmp_path / name).write_bytes(
        (_GUIDANCE / source["fixture"]).read_bytes() + b"\nProject rule: preserve my design.\n"
    )
    (finding,) = _findings(tmp_path)
    assert finding.check_id == "tickets.leftover-guidance"
    assert finding.subject == name
    assert finding.fix == (
        "move its rules into a Goalset under .booley_project/goalsets/, "
        f"then delete {tmp_path / name}"
    )
