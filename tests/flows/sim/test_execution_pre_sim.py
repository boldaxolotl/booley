"""Pre-Sim Commands live behind the Project-scoped execution seam."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.config.project_config import load_test_configuration_field, render_test_selector
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.targets.domain import TargetHandle


def _project(root: Path, marker: str) -> TargetHandle:
    project = root / ".booley_project"
    project.mkdir(parents=True)
    (project / "booley.toml").write_text(
        f'[flows.sim]\npre_run_commands = ["printf {marker} > hook.txt"]\nrun_cwd = "."\n',
        encoding="utf-8",
    )
    return cast(
        TargetHandle,
        SimpleNamespace(project_root=root.resolve(), selector="sim"),
    )


def test_each_target_handle_resolves_its_own_project_configuration(tmp_path: Path) -> None:
    first_root = tmp_path / "current"
    second_root = tmp_path / "baseline"
    first = _project(first_root, "current")
    second = _project(second_root, "baseline")

    for handle in (first, second):
        outcome = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=handle.project_root / "build",
            eda_tool="icarus",
            timeout_s=5,
        )
        assert outcome is not None
        assert outcome.status == "passed"

    assert (first_root / "hook.txt").read_text(encoding="utf-8") == "current"
    assert (second_root / "hook.txt").read_text(encoding="utf-8") == "baseline"


def test_test_registry_is_scoped_to_each_checkout(tmp_path: Path) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for root, name, selector in (
        (current, "now", "+test={name}"),
        (baseline, "then", "+case={index}"),
    ):
        project = root / ".booley_project"
        project.mkdir(parents=True)
        (project / "tests.toml").write_text(
            f'[sim]\ntests = ["{name}"]\nselect = "{selector}"\n',
            encoding="utf-8",
        )

    assert load_test_configuration_field(current, "tests") == {"sim": ["now"]}
    assert load_test_configuration_field(baseline, "tests") == {"sim": ["then"]}
    assert render_test_selector("sim", 0, "now", work_dir=current) == "+test=now"
    assert render_test_selector("sim", 0, "then", work_dir=baseline) == "+case=0"


def test_legacy_two_groups_retain_both_real_hooks_after_second_integrity_failure(
    tmp_path, monkeypatch
):
    import json
    import os

    from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
    from booley.flows.sim.flow import SimulateFlow
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_execution_engine import (
        _subprocess_invoker,
        _write_stale_compiler_fixture,
    )

    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    command = 'echo actual-hook; if [ "$BOOLEY_TEST_NAME" = b ]; then sed -i s/OLD/NEW/ tb.sv; fi'
    (project / ".booley_project/booley.toml").write_text(
        "[flows.sim]\npre_run_commands = " + json.dumps([command]) + "\n"
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project), options=SimulationOptions(timeout_ms=5000)
    )
    outcome = execution.run(handle, NamedTests(("a", "b")))
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "build"
    assert [firing.test_names for firing in outcome.pre_sim_runs] == [("a",), ("b",)]
    assert [firing.returncode for firing in outcome.pre_sim_runs] == [0, 0]
    assert all(firing.stdout_tail == "actual-hook\n" for firing in outcome.pre_sim_runs)
    lines = SimulateFlow._pre_sim_output_lines(outcome)
    assert "for sim_a/a: rc=0" in lines[0] and "for sim_a/b: rc=0" in lines[1]
