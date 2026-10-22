"""Semantic deltas use real Target inspection, runtime test lookup and typed values."""

import json
from pathlib import Path

import pytest

from booley.goals.finish import finish_goal
from booley.goals.target_changes import resolved_surfaces
from booley.targets.goal_diff import semantic_target_changes
from tests.goals.conftest import TOP_CORE, enter_goals, git
from tests.goals.test_finish import environment, request
from tests.goals.test_status import publish
from tests.target_test_support import write_two_core_sim_project


def test_runtime_target_declarations_and_qualified_test_lookup(tmp_path):
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_bytes(b"[project]\n")
    (project / "tests.toml").write_bytes(b'["::top:0#top"]\ntests=["smoke"]\n')
    (tmp_path / "rtl.v").write_bytes(b"module top; endmodule\n")
    core = tmp_path / "top.core"
    core.write_bytes(
        TOP_CORE.replace("toplevel: top}", "toplevel: top, parameters: [width]}").encode()
        + b"parameters:\n  width: {datatype: int, paramtype: vlogparam, default: 1}\n"
    )
    before = resolved_surfaces(tmp_path)
    assert before["::top:0#top"]["tests"]["tests"] == ["smoke"]
    assert before["::top:0#top"]["parameter_declarations"]["width"]["default"] == 1
    core.write_bytes(TOP_CORE.encode())
    after = resolved_surfaces(tmp_path)
    change = semantic_target_changes(before, after)[0]
    assert "parameter_declarations" in change["fields"]
    assert change["after"]["parameter_declarations"] == {}
    core.write_bytes(TOP_CORE.replace("top: {filesets:", "other: {filesets:").encode())
    rows = semantic_target_changes(after, resolved_surfaces(tmp_path))
    assert {(row["target"], row["change"]) for row in rows} == {
        ("::top:0#top", "removed"),
        ("::top:0#other", "added"),
    }


def test_evidence_digest_uses_each_cores_own_section(tmp_path):
    write_two_core_sim_project(
        tmp_path,
        '["vendor:a:x#sim"]\ntests = ["a_test"]\n["vendor:b:y#sim"]\ntests = ["b_test"]\n',
    )
    surfaces = resolved_surfaces(tmp_path)
    assert surfaces["vendor:a:x:1.0#sim"]["tests"]["tests"] == ["a_test"]
    assert surfaces["vendor:b:y:1.0#sim"]["tests"]["tests"] == ["b_test"]


@pytest.mark.parametrize("before,after", [(1, 1.0), (1, True), (1.0, True)])
def test_type_exact_parameter_and_define_values_are_modified(before, after):
    rows = semantic_target_changes(
        {"target": {"parameters": {"width": before}, "defines": {1: "integer", "mixed": before}}},
        {"target": {"parameters": {"width": after}, "defines": {1: "integer", "mixed": after}}},
    )
    assert rows[0]["change"] == "modified"
    assert rows[0]["fields"] == ["defines", "parameters"]
    assert json.loads(json.dumps(rows, sort_keys=True)) == rows


# Windows CI has measured over 40% of the 60 s default; keep 3x headroom (#1202).
@pytest.mark.timeout(120)
def test_constraints_and_resolved_target_modifications_are_frozen_in_completion(layout):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    (layout.worktree / "timing.sdc").write_bytes(b"create_clock -period 10 clk\n")
    (layout.worktree / "pins.xdc").write_bytes(b"set_property PACKAGE_PIN A1 [get_ports clk]\n")
    core = layout.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(b"toplevel: top}", b"toplevel: top, parameters: [FEATURE]}")
        + b"parameters:\n  FEATURE: {datatype: int, paramtype: vlogdefine, default: 1}\n"
    )
    git(layout.worktree, "add", "timing.sdc", "pins.xdc", "top.core")
    git(layout.worktree, "commit", "-qm", "final target and constraints")
    publish(layout)
    result = finish_goal(request(layout), environment(layout))
    facts = json.loads(Path(result["package"]).read_bytes())
    assert {row["path"] for row in facts["constraint_edits"]} == {"rtl/timing.sdc", "rtl/pins.xdc"}
    assert facts["goals"][0]["modified_target"]
    assert "FEATURE" in facts["target_changes"][0]["after"]["defines"]


@pytest.mark.parametrize("kind", ["added", "removed", "modified"])
def test_artifact_projection_preserves_authored_delta_and_epoch_membership(kind):
    from copy import deepcopy

    from booley.goals.target_changes import project_target_changes

    surface = {
        "filesets": [{"path": "other.vmem", "type": "user"}, {"path": "kept.hex", "type": "user"}],
        "parameters": {"message": "other.vmem", "width": True},
    }
    row = {
        "target": "target",
        "change": kind,
        "fields": ["filesets", "parameters"],
        "before": None if kind == "added" else deepcopy(surface),
        "after": None if kind == "removed" else deepcopy(surface),
    }
    original = deepcopy(row)
    projected = project_target_changes([row], frozenset(), frozenset({"other.vmem"}))[0]
    assert row == original
    assert projected["change"] == kind and projected["fields"] == row["fields"]
    assert projected["before"] == row["before"]
    if projected["after"] is not None:
        assert projected["after"]["filesets"] == [{"path": "kept.hex", "type": "user"}]
        assert projected["after"]["parameters"] == surface["parameters"]
    assert project_target_changes([row], frozenset(), frozenset()) == [row]


def test_changed_revision_has_no_current_candidate_and_keeps_declared_params(goal_mode):
    from booley.goals.state_store import load_goal_state
    from booley.goals.store import GoalStore
    from tests.goals.conftest import LINT_KEY, bump_spec

    publish(goal_mode)
    bump_spec(goal_mode, LINT_KEY)
    store = GoalStore(goal_mode.control)
    state = load_goal_state(store, store.load(goal_mode.record.id))
    assert state.criteria[LINT_KEY].met is False
    assert state.criteria[LINT_KEY].detail == {}
    assert state.criteria[LINT_KEY].params["target"] == "top"
