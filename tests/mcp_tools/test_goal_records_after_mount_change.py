"""Stored Goal records and waiver proposals keep resolving after a Sandbox mount point change.

The criterion tests of issue 4088 live here, not under ``tests/goals``, because
the broad run never collects that directory (``tests/conftest.py``).
"""

# ruff: noqa: F811  (the Goal fixtures are imported, then named as test arguments)
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.goals.apply import recover
from booley.goals.binding import bind_run
from booley.goals.paths import record_paths
from booley.goals.proposals import digest, load_proposal
from booley.goals.state_store import GoalStatePersistence, load_goal_state
from booley.goals.store import GoalStore
from booley.mcp import job_results
from booley.runtime.job_records import JobRecord
from tests.goals.conftest import goal_mode, layout  # noqa: F401  (fixtures)
from tests.goals.test_goal_waivers import KEY, prepared, propose
from tests.goals.test_proposals import approve, interrupted_transaction
from tests.mount_change_support import respell, simulate_mount_change


def test_job_binding_under_former_mount_resolves_current_goal_directories(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    former = simulate_mount_change(monkeypatch, goal_mode.control, tmp_path / "former-mount")
    raw = bind_run(GoalStore(goal_mode.control), goal_mode.worktree, "run-1").to_json()
    raw["booley_dir"] = former
    raw["worktree_root"] = f"{former}/worktrees/wt"
    runtime_dir = record_paths(goal_mode.control, goal_mode.record.id).runtime_dir
    (runtime_dir / "flow-reports").mkdir(parents=True, exist_ok=True)
    located = job_results.LocatedJob(
        JobRecord("run-1", "sim", "t", 60, binding=raw), root=goal_mode.control
    )
    repository = job_results.JobRepository()

    assert repository.report_booley_dir(located) == goal_mode.control
    assert repository.report_dirs(located) == (runtime_dir / "flow-reports",)
    binding = job_results.job_binding(located.record)
    assert binding is not None
    assert binding.worktree_root == goal_mode.control / "worktrees" / "wt"
    assert GoalStore(binding.booley_dir).load(binding.record_id).id == goal_mode.record.id


def test_goal_record_and_state_under_former_mount_resolve_current_worktree(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = goal_mode.control.resolve()
    former = simulate_mount_change(monkeypatch, current, tmp_path / "former-mount")
    binding = bind_run(GoalStore(goal_mode.control), goal_mode.worktree, "run-1")
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    state = DevelopmentState.load(paths.state_file, GoalStatePersistence(binding))
    state.work_dir = str(current / "worktrees" / "wt")
    state.save()
    respell(paths.record_file, current, former)
    respell(paths.state_file, current, former)
    escaped = json.dumps(former)[1:-1]
    assert escaped in paths.record_file.read_text(encoding="utf-8")
    assert escaped in paths.state_file.read_text(encoding="utf-8")

    record = GoalStore(goal_mode.control).load(goal_mode.record.id)
    loaded = load_goal_state(GoalStore(goal_mode.control), record)

    assert Path(record.worktree_path) == goal_mode.worktree.resolve()
    assert loaded.work_dir == str(current / "worktrees" / "wt")


def _under(former: str, layout, path: str) -> str:
    return former + path.removeprefix(str(layout.control.resolve()))


def _rebuild_policy(layout, policy, former):
    value = {key: item for key, item in policy.items() if key != "digest"}
    value["root"] = _under(former, layout, value["root"])
    return {**value, "digest": digest(value)}


def _respell_proposal(layout, proposal_id, former):
    """Rewrite a proposal as a writer under the former mount point stored it."""
    from booley.goals.proposals import Proposal, proposal_path

    root = proposal_path(record_paths(layout.control, layout.record.id).root, proposal_id)
    payload = json.loads((root / "payload.json").read_bytes())
    old = Proposal.from_json(payload).payload_digest
    waiver = payload["waiver"]
    for name in ("approval_root", "anchor_root", "candidate_root"):
        waiver[name] = _under(former, layout, waiver[name])
    waiver["policy"] = _rebuild_policy(layout, waiver["policy"], former)
    new = Proposal.from_json(payload).payload_digest
    (root / "payload.json").write_bytes(json.dumps(payload).encode("utf-8"))
    state = root / "state.json"
    state.write_bytes(state.read_bytes().replace(old.encode(), new.encode()))
    return root, old, new


def _respell_producer_policy(layout, former):
    state_file = record_paths(layout.control, layout.record.id).state_file
    document = json.loads(state_file.read_bytes())
    detail = document["criteria"][KEY]["detail"]
    detail["goal_waiver_policy"] = _rebuild_policy(layout, detail["goal_waiver_policy"], former)
    state_file.write_bytes(json.dumps(document).encode("utf-8"))


def test_pending_waiver_proposal_bound_under_former_mount_applies(layout, monkeypatch, tmp_path):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    former = simulate_mount_change(
        monkeypatch, layout.control.resolve(), tmp_path / "former-mount"
    )
    _respell_proposal(layout, view.proposal.id, former)
    _respell_producer_policy(layout, former)
    stored = load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id)
    assert stored.proposal.waiver.approval_root.startswith(former)

    approve(layout, env, view.proposal.id)

    assert list(root.rglob("*.toml")) and list(root.rglob("*.md"))
    assert not Path(former).exists()
    assert env.waivers._current_waivers().waivers[0].waiver_id == candidate


def test_interrupted_waiver_apply_recovers_after_mount_change(layout, monkeypatch, tmp_path):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    path, _transaction = interrupted_transaction(layout, env, view)
    current = layout.control.resolve()
    former = simulate_mount_change(monkeypatch, current, tmp_path / "former-mount")
    proposal_root, old, new = _respell_proposal(layout, view.proposal.id, former)
    respell(path, current, former)
    path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))
    state = proposal_root / "state.json"
    stored = json.loads(state.read_bytes())
    old_transaction = stored["transaction_digest"]
    new_transaction = digest(json.loads(path.read_bytes()))
    state.write_bytes(
        state.read_bytes().replace(old_transaction.encode(), new_transaction.encode())
    )
    assert all(
        effect["path"].startswith(former) for effect in json.loads(path.read_bytes())["effects"]
    )

    recover(env.store, layout.record.id, env)

    assert list(root.rglob("*.toml")) and list(root.rglob("*.md"))
    assert (
        load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id).state
        == "applied"
    )
