"""Final-report coverage is based on Git, including both Ticket repositories."""

import json
import subprocess

import pytest

from booley.harness.scope_policy import committed_deviations
from booley.mcp.report_changes import changed_ticket_paths, validate_justifications
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.ticket_repositories import TicketWorkspaceError
from booley.ticket_board.acceptance_basis import AcceptanceBasis, BasisParticipant
from booley.ticket_board.frontmatter import format_frontmatter


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@test", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout.strip()


def init(repo):
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "old.txt").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return git(repo, "rev-parse", "HEAD")


def test_pinned_bases_cover_rename_deletion_and_paired_repository(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    outer_sha = init(outer)
    source = tmp_path / "project-source"
    project_sha = init(source)
    paired = outer / PROJECT_DIR_NAME
    git(source, "worktree", "add", "-b", "ticket", str(paired))
    git(outer, "mv", "old.txt", "renamed.txt")
    git(outer, "commit", "-qm", "rename")
    git(paired, "rm", "old.txt")
    (paired / "extra file.txt").write_text("new")
    git(paired, "add", ".")
    git(paired, "commit", "-qm", "project changes")
    basis = AcceptanceBasis(
        tuple(
            BasisParticipant(
                role=role,
                authoring_sha=sha,
                ticket_ref=f"refs/heads/booley-generation/0123456789abcdef/{role}",
                destination_ref="refs/heads/main",
                destination_sha=sha,
            )
            for role, sha in [("outer", outer_sha), ("project", project_sha)]
        )
    )
    ticket = tmp_path / "ticket.md"
    ticket.write_text(format_frontmatter({"acceptance_basis": basis.as_dict()}, ""))
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    monkeypatch.setenv("BOOLEY_PAIRED_PROJECT_REPOSITORY", "1")
    paths = changed_ticket_paths(outer)
    assert paths == sorted(
        [
            "old.txt",
            "renamed.txt",
            f"{PROJECT_DIR_NAME}/old.txt",
            f"{PROJECT_DIR_NAME}/extra file.txt",
        ]
    )
    reasons = dict.fromkeys(paths, "Required for the change")
    assert validate_justifications(json.dumps(reasons), paths) == reasons
    assert committed_deviations(outer, outer_sha, ["renamed.txt"]) == (["old.txt"], [])
    # Paired paths are classified in Ticket coordinates, not repository-local ones.
    deviations, protected = committed_deviations(
        paired, project_sha, [], path_prefix=PROJECT_DIR_NAME
    )
    assert deviations == []
    assert set(protected) == {f"{PROJECT_DIR_NAME}/old.txt", f"{PROJECT_DIR_NAME}/extra file.txt"}
    del reasons["old.txt"]
    with pytest.raises(ValueError, match=r"Missing.*old.txt"):
        validate_justifications(json.dumps(reasons), paths)


def test_missing_comparison_base_fails_closed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init(repo)
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    with pytest.raises(TicketWorkspaceError, match="Cannot determine changed files"):
        changed_ticket_paths(repo)


def test_no_changes_still_require_explicit_empty_justifications():
    assert validate_justifications("{}", []) == {}
    with pytest.raises(ValueError, match="required"):
        validate_justifications(None, [])
    with pytest.raises(ValueError, match="unknown paths"):
        validate_justifications('{"imaginary.txt":"reason"}', [])
