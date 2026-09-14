"""Cleanup without merge retains accepted source commits across retries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import cleanup_only, operations
from booley.ticket_board.acceptance_basis import AcceptanceBasis, BasisParticipant


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _ticket(tmp_path: Path) -> tuple[Path, AcceptanceBasis, dict[str, str]]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    branch = "booley-generation/0123456789abcdef/test-cleanup"
    worktree = tmp_path / "ticket-worktree"
    _git(root, "worktree", "add", "-b", branch, str(worktree), "main")
    (worktree / "README.md").write_text("accepted change\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "accepted")
    source = _git(worktree, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                base,
                f"refs/heads/{branch}",
                "refs/heads/main",
                base,
            ),
        )
    )
    return root, basis, {"outer": source}


def test_cleanup_only_pins_source_then_deletes_unmerged_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, basis, sources = _ticket(tmp_path)
    state = {"status": "review", "approvals": 0}

    def approve(_tio, _slug, **_kwargs):
        state["status"] = "done"
        state["approvals"] += 1
        return True

    monkeypatch.setattr(operations, "_approve_transition", approve)
    tio = SimpleNamespace(
        _project_root=root, find_ticket=lambda _slug: {"status": state["status"]}
    )

    assert cleanup_only.advance_cleanup_only(tio, "test-cleanup", basis, sources)
    assert _git(root, "rev-parse", "HEAD") == basis.outer_sha
    assert cleanup_only.cleanup_only_sources(root, "test-cleanup", basis, sources) == sources
    assert cleanup_only._ref_sha(root, basis.participants[0].ticket_ref) is None
    assert (
        json.loads(
            cleanup_only._journal_path(root, "test-cleanup", basis).read_text(encoding="utf-8")
        )["state"]
        == "done"
    )
    assert cleanup_only.advance_cleanup_only(tio, "test-cleanup", basis, sources)
    assert state["approvals"] == 1


def test_cleanup_only_recovers_after_pinning_before_branch_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, basis, sources = _ticket(tmp_path)
    tio = SimpleNamespace(_project_root=root, find_ticket=lambda _slug: {"status": "done"})
    retire = cleanup_only._retire_participant
    interrupted = False

    def fail_once(*args):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("interrupted after pins")
        return retire(*args)

    monkeypatch.setattr(cleanup_only, "_retire_participant", fail_once)
    with pytest.raises(OSError, match="interrupted after pins"):
        cleanup_only.advance_cleanup_only(tio, "test-cleanup", basis, sources)
    assert cleanup_only.cleanup_only_sources(root, "test-cleanup", basis, sources) == sources
    assert cleanup_only._ref_sha(root, basis.participants[0].ticket_ref) == sources["outer"]

    assert cleanup_only.advance_cleanup_only(tio, "test-cleanup", basis, sources)
    assert cleanup_only._ref_sha(root, basis.participants[0].ticket_ref) is None


def test_op_complete_routes_authored_cleanup_without_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, basis, sources = _ticket(tmp_path)
    state = {"status": "review"}
    policy = {
        "destination": "review",
        "merge": False,
        "cleanup": True,
        "triage_report": False,
    }
    tio = SimpleNamespace(
        _project_root=root,
        find_ticket=lambda _slug: {
            "status": state["status"],
            "file": "board/review/test-cleanup.md",
            "on_success": policy,
        },
        load_basis=lambda _slug: basis,
    )
    monkeypatch.setattr(
        operations,
        "_completion_acceptance_valid",
        lambda *_args: SimpleNamespace(participant_heads=sources),
    )
    monkeypatch.setattr(
        operations,
        "_approve_transition",
        lambda *_args, **_kwargs: state.update(status="done") or True,
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_args, **_kwargs: None)

    assert operations.op_complete(tio, "test-cleanup")
    assert state["status"] == "done"
    assert cleanup_only._ref_sha(root, basis.participants[0].ticket_ref) is None
    assert cleanup_only.cleanup_only_sources(root, "test-cleanup", basis, sources) == sources
