"""Bound writers retain ownership only while their original Goal Record exists."""

import json
import shutil
from dataclasses import replace

import pytest

from booley.goals.paths import record_paths
from booley.goals.store import GoalStore, GoalStoreError
from booley.mcp.call_context import CallContext
from tests.goals.conftest import bind, enter_goals


def bound_context(layout):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    binding = bind(layout)
    paths = record_paths(layout.control, binding.record_id)
    for path in (
        paths.root.parent / "history/kept.md",
        paths.root.parent / "sibling-20261006T000000Z/kept",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unselected evidence")
    return CallContext(
        layout.worktree,
        paths.jobs_dir,
        paths.state_file,
        paths.logs_dir,
        paths.runtime_dir,
        {"BOOLEY_RUNTIME_DIR": str(paths.runtime_dir)},
        binding,
    )


def fence(context, stage):
    paths = record_paths(context.binding.booley_dir, context.binding.record_id)
    data = json.loads(paths.record_file.read_bytes())
    data.update(state="failed", ended_at="2026-10-06T15:00:00Z")
    paths.record_file.write_text(json.dumps(data))
    marker = paths.root.parent / f".pruning-{context.binding.record_id}.json"
    if stage != "terminal":
        marker.write_text("captured pruning intent")
    if stage == "quarantine":
        paths.root.rename(paths.root.parent / f".pruning-{context.binding.record_id}")
    elif stage == "removed":
        shutil.rmtree(paths.root)
        marker.unlink()
    return paths


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_bound_mutation_requires_existing_unmarked_record(layout):
    from booley.goals.record_mutation import bound_record_mutation

    context = bound_context(layout)
    with bound_record_mutation(context.binding, context.jobs_root, active=True) as paths:
        assert paths.jobs_dir == context.jobs_root
    for changed in (
        replace(context.binding, goal_branch="foreign"),
        replace(context.binding, record_id="other-20261006T120000Z"),
    ):
        with pytest.raises(GoalStoreError), bound_record_mutation(changed, context.jobs_root):
            pytest.fail("mismatching binding accepted")
    with (
        pytest.raises(GoalStoreError),
        bound_record_mutation(context.binding, layout.control / "elsewhere"),
    ):
        pytest.fail("mismatching root accepted")
    with (
        pytest.raises(GoalStoreError),
        bound_record_mutation(context.binding, context.jobs_root / "../../../../escape"),
    ):
        pytest.fail("escaping destination accepted")
    paths = fence(context, "terminal")
    with bound_record_mutation(context.binding, context.jobs_root):
        assert GoalStore(layout.control).load(context.binding.record_id).state.value == "failed"
    with (
        pytest.raises(GoalStoreError),
        bound_record_mutation(context.binding, context.jobs_root, active=True),
    ):
        pytest.fail("terminal admission accepted")
    outside = layout.main / "outside"
    outside.mkdir()
    paths.runtime_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(GoalStoreError), bound_record_mutation(context.binding, context.jobs_root):
        pytest.fail("linked ancestor accepted")
    paths.runtime_dir.unlink()
    for stage in ("intent", "removed"):
        fence(context, stage)
        before = tree_bytes(layout.control / "goals")
        with (
            pytest.raises(GoalStoreError),
            bound_record_mutation(context.binding, context.jobs_root),
        ):
            pytest.fail("fenced writer accepted")
        assert tree_bytes(layout.control / "goals") == before


def test_waiting_mutation_rejects_removed_lock_inode(layout, monkeypatch):
    import threading

    from booley.goals import store as store_module
    from booley.goals.record_mutation import bound_record_mutation

    context = bound_context(layout)
    paths = fence(context, "terminal")
    opened, release = threading.Event(), threading.Event()
    errors = []
    original = store_module.wait_for_file_lock

    def wait(handle, **kwargs):
        if threading.current_thread().name == "late-writer":
            opened.set()
            assert release.wait(5)
        return original(handle, **kwargs)

    monkeypatch.setattr(store_module, "wait_for_file_lock", wait)

    def writer():
        try:
            with bound_record_mutation(context.binding, context.jobs_root):
                (paths.root / "late").write_text("unsafe")
        except GoalStoreError as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer, name="late-writer")
    thread.start()
    try:
        assert opened.wait(5)
        shutil.rmtree(paths.root)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors and not paths.root.exists()
