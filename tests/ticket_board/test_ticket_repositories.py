"""Operational Git failures never become negative ancestry verdicts."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("code", [0, 1, 128])
@pytest.mark.parametrize(
    "site", ["archive", "journal", "repositories", "workspace", "detached", "attached"]
)
def test_ancestry_sites_split_git_exit_codes(tmp_path, monkeypatch, code, site):
    calls = []

    def git(*args, **kwargs):
        command = args[1] if isinstance(args[1], list) else args[1:]
        calls.append(command)
        ancestry = "merge-base" in command
        return subprocess.CompletedProcess(
            command,
            code if ancestry else 0,
            "refs/heads/ticket" if "symbolic-ref" in command else "",
            "missing object" if ancestry else "",
        )

    operation, ctx = _operation(site, tmp_path, monkeypatch, git)
    if code == 0 or (site == "journal" and code == 1):
        operation()
    elif site in ("detached", "attached"):
        result = operation()
        assert result is not None
        assert ("cannot verify ancestry" in result.block_reason) == (code == 128)
        if site == "attached":
            assert ctx.feature_branch == "unchanged"
        assert not any("checkout" in call for call in calls)
    else:
        with pytest.raises(RuntimeError) as caught:
            operation()
        assert ("cannot verify ancestry" in str(caught.value)) == (code == 128)
        if code == 128:
            assert "missing object" in str(caught.value)
            assert "no longer descends" not in str(caught.value)


def _operation(site, tmp_path, monkeypatch, git):
    from functools import partial

    from booley.harness.setup import workspace as setup
    from booley.ticket_board import archive_generation, ticket_repositories, workspace_ops
    from booley.ticket_board.acceptance_journal import _advance

    if site == "archive":
        monkeypatch.setattr(archive_generation, "_git", git)
        monkeypatch.setattr(archive_generation, "_ref_sha", lambda *a: "child")
        monkeypatch.setattr(archive_generation, "_records", lambda *a: [])
        monkeypatch.setattr(archive_generation, "worktree_for_ref", lambda *a: None)
        operation = partial(
            archive_generation._participant,
            "outer",
            tmp_path,
            "ref",
            tmp_path / "canonical",
            "parent",
        )
    elif site == "journal":
        monkeypatch.setattr(_advance, "_git", git)
        operation = partial(_advance._is_ancestor, tmp_path, "parent", "child")
    elif site == "repositories":
        monkeypatch.setattr(ticket_repositories, "_git", git)
        monkeypatch.setattr(ticket_repositories, "_ref_sha", lambda *a: "child")
        operation = partial(
            ticket_repositories._basis_branch, tmp_path, "refs/heads/ticket", "parent"
        )
    elif site == "workspace":
        monkeypatch.setattr(workspace_ops, "_git", git)
        operation = partial(
            workspace_ops._require_ancestor, tmp_path, "parent", "child", "not descendant"
        )
    else:
        monkeypatch.setattr(setup, "git_run", git)
        basis = SimpleNamespace(
            outer_sha="parent",
            participant=lambda role: SimpleNamespace(ticket_ref="refs/heads/ticket"),
        )
        ctx = SimpleNamespace(ticket_baseline=basis, feature_branch="unchanged")
        operation = (
            partial(setup._attach_clean_detached_basis_branch, tmp_path, "refs/heads/ticket")
            if site == "detached"
            else partial(setup._attach_basis_branch, ctx, tmp_path)
        )
    return operation, ctx if site in ("attached", "detached") else None
