"""Retained records are observed without running live Goal status."""
# ruff: noqa: F811 — pytest fixture imports intentionally share parameter names.

import json
from dataclasses import replace

from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from tests.goals.test_store import _git, _record, repo  # noqa: F401 — shared fixture


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def terminal(repo, state=GoalState.FAILED, slug="orphan"):
    store = GoalStore(repo["project"])
    identity = store.identify_worktree(repo["linked"], create=True)
    rec = replace(_record(identity, f"{slug}-20261006T100000Z", repo["linked"]), revision=1)
    record_paths(store.booley_dir, rec.id).root.mkdir(parents=True)
    record_paths(store.booley_dir, rec.id).lock_file.touch()
    rec = replace(rec, state=state, ended_at="2026-10-06T11:00:00Z")
    record_paths(store.booley_dir, rec.id).record_file.write_text(json.dumps(rec.to_json()))
    return rec


def remove_checkout(repo):
    _git(repo["main"], "worktree", "remove", "--force", str(repo["linked"]))
    _git(repo["main"], "branch", "-D", "work")


def test_inventory_is_observational_for_all_states(repo):
    from booley.goals.inventory import list_records, show_record

    for state in GoalState:
        terminal(repo, state, state.value)
    before = snapshot(repo["project"])
    result = list_records(GoalStore(repo["project"]), repo["main"])
    assert {r["state"] for r in result["records"]} == {s.value for s in GoalState}
    for row in result["records"]:
        assert row["worktree_presence"] == "present"
        assert row["size_bytes"] > 0
        if row["state"] != "finished":
            assert (
                show_record(GoalStore(repo["project"]), repo["main"], row["record_id"])["record"]
                == row
            )
    assert snapshot(repo["project"]) == before


def test_presence_is_identity_aware_and_repository_specific(repo, tmp_path):
    from booley.goals.inventory import presence

    record = terminal(repo)
    moved = tmp_path / "moved"
    _git(repo["main"], "worktree", "move", str(repo["linked"]), str(moved))
    assert presence(record, repo["main"]) == ("present", "missing")
    _git(repo["main"], "branch", record.branch)
    assert presence(record, repo["main"]) == ("present", "present")
    _git(repo["main"], "worktree", "remove", "--force", str(moved))
    assert presence(record, repo["main"]) == ("missing", "present")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    assert presence(record, other) == ("unavailable", "unavailable")


def test_size_budget_links_and_corrupt_snapshot(repo, tmp_path):
    import pytest

    from booley.goals.inventory import list_records, logical_bytes, show_record
    from booley.goals.store import GoalStoreError

    record = terminal(repo)
    root = record_paths(repo["project"], record.id).root
    assert logical_bytes(root, entries=1) is None
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    before = logical_bytes(root)
    (root / "external").symlink_to(outside)
    assert logical_bytes(root) == before
    store = GoalStore(repo["project"])
    assert show_record(store, repo["main"], record.id)["goals"] == []
    (root / "booley_state.json").write_bytes(b"bad json")
    with pytest.raises(GoalStoreError):
        show_record(store, repo["main"], record.id)
    record_paths(repo["project"], record.id).record_file.write_bytes(b"bad")
    assert record.id in str(list_records(store, repo["main"])["diagnostics"])
