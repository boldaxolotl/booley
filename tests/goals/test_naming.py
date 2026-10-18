"""Catalog ambiguity affects presentation without changing persisted Goal identity."""

import asyncio
import json
from pathlib import Path

import pytest

from booley.goals import entry
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.format import render_status
from booley.goals.model import parse_goal_args
from booley.goals.naming import read_names, spec_digest
from booley.goals.paths import record_paths
from booley.goals.presentation import goal_presentation
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.harness.dashboard.model import _terminal, project_goal
from booley.mcp.goal_tools import dispatch_goal_tool
from booley.targets.catalog import TargetCatalog
from tests.goals.conftest import TOP_CORE, bind, git

QUALIFIED = "vendor:lib:top:1#top"


def _enter(layout, monkeypatch, *, collision=False, goals=None):
    core = TOP_CORE.replace("::top:0", "vendor:lib:top:1")
    (layout.worktree / "top.core").write_text(core)
    if collision:
        (layout.worktree / "other.core").write_text(
            core.replace("vendor:lib:top:1", "vendor:lib:other:1")
        )
    git(layout.worktree, "add", "*.core")
    git(layout.worktree, "commit", "-qm", "declare Targets")
    monkeypatch.setattr(entry, "TargetCatalog", TargetCatalog)
    result = enter_goal_mode(
        EntryRequest(
            layout.worktree,
            "names",
            parse_goal_args(goals or [{"family": "LINT", "target": QUALIFIED}]),
        ),
        EntryEnvironment(layout.control, routing_guidance="Explicit test routing guidance."),
    )
    layout.record = result.record
    return result


@pytest.mark.parametrize("collision, expected", [(False, "LINT:top"), (True, "LINT:" + QUALIFIED)])
def test_catalog_spelling_agrees_on_actual_entry_status_mcp_dashboard_and_producer(
    layout, monkeypatch, collision, expected
):
    result = _enter(layout, monkeypatch, collision=collision)
    store = GoalStore(layout.control)
    paths = record_paths(layout.control, layout.record.id)
    before = paths.record_file.read_bytes()
    loaded = store.load(layout.record.id)
    goal = loaded.goals[0].spec
    assert goal.target == QUALIFIED
    assert goal.key == layout.record.goals[0].spec.key
    assert goal.to_json() == layout.record.goals[0].spec.to_json()
    assert goal_presentation(goal).name == expected
    status = build_status(store, loaded)
    mcp = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(layout.worktree)}, booley_dir=layout.control
        )
    )
    assert not mcp.is_error
    for text in (
        result.render(),
        render_status((status,)),
        render_status((status,), short=True),
        mcp.value[0][0].text,
        project_goal(store, loaded).goals[0].presentation.label,
        GoalFlowExecution(bind(layout)).present_criterion(goal.key, {}).label,
    ):
        assert expected in text
        assert goal.key not in text
    assert paths.record_file.read_bytes() == before
    (layout.worktree / "top.core").unlink()
    assert goal_presentation(store.load(loaded.id).goals[0].spec).name == expected


# Windows CI has measured over 40% of the 60 s default; keep 3x headroom (#1202).
@pytest.mark.timeout(120)
def test_completion_freezes_name_without_extending_package_schema(layout, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.goals.finish import finish_goal
    from tests.goals.test_finish import environment, request

    _enter(layout, monkeypatch)
    adapter = GoalFlowExecution(bind(layout))
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    goal = layout.record.goals[0].spec
    changes = state.set_criterion(goal.key, True, detail={"warnings": 0})
    adapter.record_changes(state, changes, invocation_id="ignored", producer="lint")
    state.save()
    call = request(layout, explain_html=True)
    result = finish_goal(call, environment(layout))
    paths = record_paths(layout.control, layout.record.id)
    frozen_names = read_names(paths.root / "operations" / call.operation_id)
    assert frozen_names[spec_digest(goal)] == "top"
    package = json.loads(Path(result["package"]).read_text())
    assert package["goals"][0]["spec"] == goal.to_json()
    assert "display_target" not in json.dumps(package)
    saved = GoalStore(layout.control).load(layout.record.id)
    (layout.worktree / "top.core").unlink()
    paths.root.joinpath("goal-names.json").unlink()
    assert _terminal(GoalStore(layout.control), saved).goals[0].presentation.label == "LINT:top"
    assert finish_goal(call, environment(layout)) == result
    naming_path = paths.root / "operations" / call.operation_id / "goal-names.json"
    original = naming_path.read_bytes()
    naming_path.write_text(json.dumps({spec_digest(goal): "substituted"}))
    view = _terminal(GoalStore(layout.control), saved)
    assert view.goals[0].presentation.label == "Goal unavailable (unsupported record)"
    assert view.terminal_summary and view.goals[0].evidence is not None
    naming_path.unlink()
    view = _terminal(GoalStore(layout.control), saved)
    assert view.goals[0].presentation.label == "Goal unavailable (unsupported record)"
    assert view.terminal_summary and view.goals[0].evidence is not None
    naming_path.write_bytes(original)
    _assert_invalid_authority_is_isolated(layout, naming_path.parent, goal.key)


def _assert_invalid_authority_is_isolated(layout, directory, key):
    from booley.harness.dashboard.model import DashboardReader

    for name in ("goal-names.authority.json", "request.json"):
        artifact = directory / name
        original = artifact.read_bytes()
        artifact.write_text("[]")
        view = DashboardReader(layout.worktree, layout.control).read().goals[0]
        assert "unavailable" in view.diagnostic
        assert key not in view.diagnostic
        artifact.write_bytes(original)


def test_bare_and_qualified_alias_goals_have_distinguishing_details(layout, monkeypatch):
    _enter(
        layout,
        monkeypatch,
        goals=[{"family": "LINT", "target": "top"}, {"family": "LINT", "target": QUALIFIED}],
    )
    first, second = (goal_presentation(row.spec) for row in layout.record.goals)
    assert first.name == second.name == "LINT:top"
    assert first.details != second.details
    assert "Target selector " + QUALIFIED in second.details
    assert layout.record.goals[0].spec.key != layout.record.goals[1].spec.key


@pytest.mark.parametrize("content", ["{", "[]", '{"key": null}'])
def test_corrupt_live_names_do_not_corrupt_record_or_block_abandon(layout, monkeypatch, content):
    from uuid import uuid4

    from booley.goals.finish import finish_goal
    from booley.goals.lifecycle import LifecycleRequest
    from tests.goals.test_finish import environment

    _enter(layout, monkeypatch)
    paths = record_paths(layout.control, layout.record.id)
    before = paths.record_file.read_bytes()
    paths.root.joinpath("goal-names.json").write_text(content)
    store = GoalStore(layout.control)
    assert store.load(layout.record.id).to_json() == layout.record.to_json()
    assert not store.list_records().corrupt
    assert paths.record_file.read_bytes() == before
    call = LifecycleRequest(
        layout.worktree,
        layout.record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop this Goal Mode",
    )
    assert finish_goal(call, environment(layout))["status"] == "abandoned"


def test_display_metadata_write_failure_does_not_abort_authoritative_save(layout, monkeypatch):

    from booley.goals import naming

    _enter(layout, monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError("display storage unavailable")

    monkeypatch.setattr(naming, "atomic_replace_bytes", fail)
    store = GoalStore(layout.control)
    with store.record_lock(layout.record.id) as lock:
        saved = store.save(lock, layout.record)
    assert saved.revision == layout.record.revision + 1
    assert store.load(saved.id).to_json() == saved.to_json()


@pytest.mark.parametrize("subject", ["other", "vendor:lib:other:1#top"])
def test_display_subject_cannot_substitute_a_different_target(subject):
    from booley.goals.model import GoalFamily, GoalSpec
    from booley.goals.naming import apply_names

    spec = GoalSpec("opaque_key", GoalFamily.LINT, QUALIFIED, {}, ("ad-hoc",))
    named = apply_names(spec, {spec_digest(spec): subject})
    assert goal_presentation(named).name == "LINT:" + QUALIFIED
