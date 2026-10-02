from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import simulation_selftest

from booley.ticket_board.board_layout import required_board_directories

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="release container validation requires POSIX executables"
)


def _doctor(root: Path, output: str) -> Path:
    executable = root / "booley"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "assert sys.argv[1:] == ['doctor', '--deep', '--skip-agent-checks']\n"
        "print(os.environ['DOCTOR_OUTPUT'])\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_simulation_selftest_proves_good_and_bad_runtime_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join(
        [
            "[OK] sim self-test good case 'smoke' passes",
            "[OK] sim self-test bad case 'smoke + bad overlay' correctly graded a failure",
            "0 failed.",
        ]
    )
    monkeypatch.setenv("DOCTOR_OUTPUT", output)

    evidence = simulation_selftest.validate(
        project=tmp_path,
        booley=_doctor(tmp_path, output),
        candidate_sha="candidate-sha",
        image_digest="sha256:image",
    )

    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:image",
    }
    assert evidence["checks"] == [
        {"id": "simulation-selftest.good", "status": "pass"},
        {"id": "simulation-selftest.bad-overlay", "status": "pass"},
        {"id": "doctor.summary", "status": "pass"},
    ]


def test_simulation_selftest_rejects_false_passing_bad_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join(
        [
            "[OK] sim self-test good case 'smoke' passes",
            "[XX] sim self-test bad case 'smoke + bad overlay' FALSE-PASSED",
            "1 failed.",
        ]
    )
    monkeypatch.setenv("DOCTOR_OUTPUT", output)

    with pytest.raises(RuntimeError, match="bad runtime overlay"):
        simulation_selftest.validate(
            project=tmp_path,
            booley=_doctor(tmp_path, output),
            candidate_sha="candidate-sha",
            image_digest="sha256:image",
        )


def test_prepare_clone_state_restores_init_owned_local_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    (project / ".booley_project").mkdir()

    simulation_selftest._prepare_clone_state(project)

    configured = subprocess.run(
        ["git", "config", "--local", "--get", "gc.worktreePruneExpire"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    assert configured.stdout.strip() == "never"
    tickets = project / ".booley_project" / "tickets"
    board = required_board_directories(tickets)
    assert all(directory.is_dir() for directory in (*board, tickets / "logs", tickets / "locks"))


def test_prepare_clone_state_guards_nested_project_repository(tmp_path: Path) -> None:
    """Doctor's prune guard also checks a standalone nested Project repo (#915)."""
    project = tmp_path / "project"
    project_dir = project / ".booley_project"
    project_dir.mkdir(parents=True)
    for repository in (project, project_dir):
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    simulation_selftest._prepare_clone_state(project)

    for repository in (project, project_dir):
        configured = subprocess.run(
            ["git", "config", "--local", "--get", "gc.worktreePruneExpire"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        assert configured.stdout.strip() == "never", repository
