from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import demo_surface


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_demo_surface_uses_public_commands_without_mutating_ticket(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    state = tmp_path / "state"
    ticket = state / "tickets/board/queue/release-smoke.md"
    project.mkdir()
    ticket.parent.mkdir(parents=True)
    ticket.write_text("release ticket\n", encoding="utf-8")
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
    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:image",
    }
    assert evidence["checks"][-1] == {"id": "demo.ticket-immutable", "status": "pass"}
    assert evidence["identity"] == {"uid": os.getuid(), "gid": os.getgid()}
