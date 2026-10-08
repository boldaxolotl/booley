"""Opt-in Goal Mode round trips against the production Sandbox Image."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from booley.criteria.state import DevelopmentState
from booley.goals.model import GoalState, parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))
from goal_mode_driver import (
    GoalToolRefusalError,
    Workspace,
    client_session,
    create_workspace,
    enter,
    finish,
    run_flow,
    status_rows,
    tool_text,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOLEY_GOAL_MODE_SMOKE") != "1",
    reason="requires the production image, EDA toolchain, and /opt/pdk mount",
)
_FIXTURE = Path(__file__).parents[1] / "fixtures" / "goal_mode_smoke"


def _run_git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True, timeout=30
    )


def test_fresh_asic_scaffold_has_clean_timing_baseline(tmp_path: Path) -> None:
    project = tmp_path / "scaffold-project"
    choices = ScaffoldChoices(
        name="fixture",
        sim_eda_tool="verilator",
        tb_style="sv",
        lint_eda_tool="verilator",
        asic=True,
        fpga_part=None,
    )
    for relative, content in scaffold_files(choices).items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(project, "init", "-b", "main")
    _run_git(project, "config", "user.name", "Booley Smoke")
    _run_git(project, "config", "user.email", "smoke@example.invalid")
    _run_git(project, "add", ".")
    _run_git(project, "commit", "-m", "Initialize scaffold smoke fixture")

    report_dir = project / "reports"
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project / ".booley_project")
    result = subprocess.run(
        [
            "booley",
            "flow",
            "synth",
            "--target",
            "synth",
            "--report-dir",
            str(report_dir),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "RESULT: PASS" in output
    assert "RESULT: WARN" not in output
    report = json.loads((report_dir / "synth_synth.json").read_text(encoding="utf-8"))
    assert report["whs_ns"] >= 0
    log = Path(report["artifacts"]["log"])
    if not log.is_absolute():
        log = project / log
    assert "STA-0441" not in log.resolve().read_text(encoding="utf-8")


def _workspace(tmp_path: Path) -> Workspace:
    source = tmp_path / "source"
    shutil.copytree(_FIXTURE, source)
    _run_git(source, "init", "-b", "main")
    _run_git(source, "config", "user.name", "Booley Smoke")
    _run_git(source, "config", "user.email", "smoke@example.invalid")
    (source / ".git/info/exclude").write_text("/.booley_project\n")
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-m", "Initialize smoke RTL")
    project = source / ".booley_project"
    _run_git(project, "init", "-b", "main")
    _run_git(project, "config", "user.name", "Booley Smoke")
    _run_git(project, "config", "user.email", "smoke@example.invalid")
    no_global_ignore = tmp_path / "no-global-ignore"
    no_global_ignore.write_text("")
    _run_git(project, "config", "core.excludesfile", str(no_global_ignore))
    _run_git(project, "add", ".")
    _run_git(project, "commit", "-m", "Initialize smoke Project")
    owned = tmp_path / "owned"
    owned.mkdir()
    return create_workspace(source, project, owned)


async def _enter(client: Client, workspace: Workspace, goals: list[dict[str, Any]]) -> str:
    entry = await enter(client, workspace.worktree, parse_goal_args(goals))
    record = GoalStore(workspace.project_dir).active_for_worktree(workspace.worktree)
    assert record is not None and record.id == entry["record_id"]
    assert record.state is GoalState.ACTIVE
    return record.id


def _assert_openroad(state: DevelopmentState) -> None:
    entry = state.criteria["synthesis_ok_synth_smoke"]
    assert entry.met
    detail = entry.detail
    assert detail["synth_mode"] == "physical"
    assert detail["area_source"] == "openroad_post_optimization"
    assert detail["ppa_complete"] is True and detail["timing_complete"] is True
    assert isinstance(detail["cells"], int) and detail["cells"] > 0
    clock = detail["per_clock"]["clk_i"]
    assert isinstance(clock["critical_path_ps"], int | float)
    assert isinstance(clock["fmax_mhz"], int | float)
    checks = {check["param"]: check for check in detail["checks"]}
    for parameter in ("cell_count_max", "clk_i.fmax_mhz_min"):
        assert checks[parameter]["pass"] is True
        assert checks[parameter].get("skipped") is not True
    assert not detail.get("infra_error")


async def _success(workspace: Workspace) -> None:
    worktree = workspace.worktree
    goals = [
        {"family": "lint", "target": "lint_smoke"},
        {"family": "sim", "target": "sim_smoke"},
        {
            "family": "synth",
            "target": "synth_smoke",
            "thresholds": {
                "cell_count_max": 500,
                "clk_i.fmax_mhz_min": 1,
            },
        },
    ]
    keys = ["lint_clean_lint_smoke", "sim_pass_sim_smoke", "synthesis_ok_synth_smoke"]
    async with client_session(workspace) as client:
        record_id = await _enter(client, workspace, goals)
        for goal in goals:
            await run_flow(
                client, goal["family"], {"target": goal["target"], "work_dir": str(worktree)}
            )
        status = await tool_text(client, "goal_status", {"work_dir": str(worktree)})
        assert set(status_rows(status, record_id, keys).values()) == {"met"}
        testbench = worktree / "tb/tb_dut.sv"
        with testbench.open("a", encoding="utf-8") as stream:
            stream.write("\n// freshness probe\n")
        stale = status_rows(
            await tool_text(client, "goal_status", {"work_dir": str(worktree)}), record_id, keys
        )
        assert stale["sim_pass_sim_smoke"] == "stale"
        assert stale["lint_clean_lint_smoke"] == stale["synthesis_ok_synth_smoke"] == "met"
        _run_git(worktree, "add", "tb/tb_dut.sv")
        _run_git(worktree, "commit", "-m", "test: add freshness probe")
        await run_flow(client, "sim", {"target": "sim_smoke", "work_dir": str(worktree)})
        assert set(
            status_rows(
                await tool_text(client, "goal_status", {"work_dir": str(worktree)}),
                record_id,
                keys,
            ).values()
        ) == {"met"}
        await finish(client, worktree, record_id)
    store = GoalStore(workspace.project_dir)
    assert store.load(record_id).state is GoalState.FINISHED
    _assert_openroad(DevelopmentState.load(record_paths(store.project_dir, record_id).state_file))


def test_goal_mode_success_staleness_and_openroad(tmp_path: Path) -> None:
    asyncio.run(_success(_workspace(tmp_path)))


async def _failure(workspace: Workspace) -> None:
    worktree = workspace.worktree
    async with client_session(workspace) as client:
        record_id = await _enter(client, workspace, [{"family": "sim", "target": "sim_fail"}])
        text = await run_flow(
            client, "sim", {"target": "sim_fail", "work_dir": str(worktree)}, expected_exit_code=1
        )
        assert "EXIT_CODE: 1" in text and "intentional Goal Mode smoke failure" in text
        status = await tool_text(client, "goal_status", {"work_dir": str(worktree)})
        assert status_rows(status, record_id, ["sim_pass_sim_fail"]) == {
            "sim_pass_sim_fail": "unmet"
        }
        before = _run_git(worktree, "rev-parse", "HEAD").stdout
        with pytest.raises(GoalToolRefusalError) as refusal:
            await finish(client, worktree, record_id)
        assert "sim_pass_sim_fail" in str(refusal.value)
        assert _run_git(worktree, "rev-parse", "HEAD").stdout == before
        assert GoalStore(workspace.project_dir).load(record_id).state is GoalState.ACTIVE


def test_goal_mode_failing_sim_refuses_finish(tmp_path: Path) -> None:
    asyncio.run(_failure(_workspace(tmp_path)))
