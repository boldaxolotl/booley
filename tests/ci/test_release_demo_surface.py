from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import demo_surface

from booley.ticket_board.board_layout import (
    StateRecord,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.lifecycle import TicketState

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="release container validation requires POSIX executables"
)


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _queued_ticket(tmp_path: Path) -> tuple[Path, Path]:
    """Return (project, project state) holding the queued Ticket ``release-smoke``."""
    project = tmp_path / "project"
    state = tmp_path / "state"
    tickets_dir = state / "tickets"
    project.mkdir()
    ticket = ticket_document_path(tickets_dir, "release-smoke")
    ticket.parent.mkdir(parents=True)
    state_record_path(tickets_dir, "release-smoke").parent.mkdir(parents=True)
    ticket.write_text("release ticket\n", encoding="utf-8")
    write_state_record(tickets_dir, "release-smoke", StateRecord.fresh(TicketState.QUEUED))
    return project, state


def test_demo_surface_uses_public_commands_without_mutating_ticket(
    tmp_path: Path, monkeypatch
) -> None:
    project, state = _queued_ticket(tmp_path)
    ticket = ticket_document_path(state / "tickets", "release-smoke")
    record = state_record_path(state / "tickets", "release-smoke")
    record_before = record.read_bytes()
    command_log = tmp_path / "commands.jsonl"
    monkeypatch.setenv("COMMAND_LOG", str(command_log))
    monkeypatch.setenv("EXPECTED_VERSION", "1.2.3")
    logger = (
        "import json, os, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    python = _executable(
        tmp_path / "python",
        logger + "if '-c' in sys.argv and 'booley.__version__' in sys.argv[-1]:\n"
        "    print(os.environ['EXPECTED_VERSION'])\n",
    )
    booley = _executable(tmp_path / "booley", logger)

    evidence = demo_surface.validate(
        project=project,
        project_state=state,
        ticket_slug="release-smoke",
        expected_version="1.2.3",
        python=python,
        booley=booley,
        candidate_sha="candidate-sha",
        image_digest="sha256:image",
    )

    commands = [json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()]
    assert ["-I", "-m", "booley.ticket_board", "validate-ticket", str(ticket)] in commands
    assert ["-I", "-m", "booley.ticket_board", "show", "release-smoke"] in commands
    assert ["board", "show"] in commands
    assert ticket.read_text(encoding="utf-8") == "release ticket\n"
    assert record.read_bytes() == record_before
    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:image",
    }
    assert evidence["checks"][-1] == {"id": "demo.ticket-immutable", "status": "pass"}
    assert evidence["identity"] == {"uid": os.getuid(), "gid": os.getgid()}


def _queued_demo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Return (project, state, fake python, fake booley) for a queued demo Ticket."""
    project, state = _queued_ticket(tmp_path)
    python = _executable(tmp_path / "python", "import sys\nprint('1.2.3')\n")
    booley = _executable(tmp_path / "booley", "")
    return project, state, python, booley


def _validate(project: Path, state: Path, python: Path, booley: Path) -> dict[str, object]:
    return demo_surface.validate(
        project=project,
        project_state=state,
        ticket_slug="release-smoke",
        expected_version="1.2.3",
        python=python,
        booley=booley,
        candidate_sha="candidate-sha",
        image_digest="sha256:image",
    )


def test_demo_surface_requires_a_queued_state_record(tmp_path: Path) -> None:
    project, state, python, booley = _queued_demo(tmp_path)
    state_record_path(state / "tickets", "release-smoke").unlink()

    with pytest.raises(ValueError, match="is draft, not queued"):
        _validate(project, state, python, booley)


def test_demo_surface_rejects_a_state_transition(tmp_path: Path) -> None:
    project, state, python, _ = _queued_demo(tmp_path)
    tickets_dir = state / "tickets"
    record = state_record_path(tickets_dir, "release-smoke")
    # A stdlib-only stand-in for a command that starts the Ticket running.
    starts_ticket = (
        "import json, pathlib\n"
        f"record = pathlib.Path({str(record)!r})\n"
        "value = json.loads(record.read_text())\n"
        "value['state'] = 'running'\n"
        "record.write_text(json.dumps(value))\n"
    )
    booley = _executable(tmp_path / "booley", starts_ticket)

    with pytest.raises(RuntimeError, match="mutated the queued ticket"):
        _validate(project, state, python, booley)


def test_demo_surface_main_defaults_to_running_python(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_validate(**kwargs):
        captured.update(kwargs)
        return {"schema": 1}

    evidence = tmp_path / "evidence.json"
    monkeypatch.setattr(demo_surface, "validate", fake_validate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "demo_surface.py",
            "--project",
            str(tmp_path),
            "--project-state",
            str(tmp_path),
            "--ticket-slug",
            "release-smoke",
            "--expected-version",
            "1.2.3",
            "--image-digest",
            "sha256:image",
            "--evidence",
            str(evidence),
        ],
    )

    assert demo_surface.main() == 0
    assert captured["python"] == Path(sys.executable)
