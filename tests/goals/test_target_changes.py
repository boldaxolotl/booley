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


def test_runtime_target_declarations_and_qualified_test_lookup(tmp_path):
    project = tmp_path / ".booley_project"
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


@pytest.mark.parametrize("before,after", [(1, 1.0), (1, True), (1.0, True)])
def test_type_exact_parameter_and_define_values_are_modified(before, after):
    rows = semantic_target_changes(
        {"target": {"parameters": {"width": before}, "defines": {1: "integer", "mixed": before}}},
        {"target": {"parameters": {"width": after}, "defines": {1: "integer", "mixed": after}}},
    )
    assert rows[0]["change"] == "modified"
    assert rows[0]["fields"] == ["defines", "parameters"]
    assert json.loads(json.dumps(rows, sort_keys=True)) == rows


def test_constraints_and_resolved_target_modifications_are_frozen_in_completion(layout):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
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
