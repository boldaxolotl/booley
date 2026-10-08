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
from uuid import uuid4

import pytest
from mcp import Client, StdioServerParameters

from booley.criteria.state import DevelopmentState
from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))
from goal_mode_driver import run_flow, status_rows, tool_text

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


def _worktree(tmp_path: Path) -> Path:
    primary = tmp_path / "primary"
    shutil.copytree(_FIXTURE, primary)
    _run_git(primary, "init", "-b", "main")
    _run_git(primary, "config", "user.name", "Booley Smoke")
    _run_git(primary, "config", "user.email", "smoke@example.invalid")
    with (primary / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("\n/.booley_project/\n")
    _run_git(primary, "add", ".")
    _run_git(primary, "commit", "-m", "Initialize Goal Mode smoke fixture")
    worktree = tmp_path / "goal-worktree"
    _run_git(primary, "worktree", "add", "--detach", str(worktree))
    shutil.copytree(primary / ".booley_project", worktree / ".booley_project")
    return worktree


def _server(worktree: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "booley.mcp.server"],
        cwd=str(worktree),
        env=os.environ
        | {
            "BOOLEY_PROJECT_DIR": str(worktree / ".booley_project"),
            "BOOLEY_MCP_MODE": "interactive",
            "BOOLEY_GOAL_MODE_PREVIEW": "1",
            "BOOLEY_IN_SANDBOX": "1",
        },
    )


async def _enter(client: Client, worktree: Path, goals: list[dict[str, Any]]) -> str:
    text = await tool_text(
        client,
        "goal_enter",
        {
            "work_dir": str(worktree),
            "slug": "smoke",
            "goals": goals,
        },
    )
    record = GoalStore(worktree / ".booley_project").active_for_worktree(worktree)
    assert record is not None, text
    assert record.state is GoalState.ACTIVE
    return record.id


async def _finish(client: Client, worktree: Path, record_id: str) -> str:
    return await tool_text(
        client,
        "goal_finish",
        {
            "work_dir": str(worktree),
            "record_id": record_id,
            "operation_id": str(uuid4()),
            "summary": "Production-image smoke exercised real lint, simulation, staleness and OpenROAD synthesis.",
        },
    )


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


async def _success(worktree: Path) -> None:
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
    async with Client(_server(worktree), mode="2026-07-28") as client:
        assert client.protocol_version == "2026-07-28"
        record_id = await _enter(client, worktree, goals)
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
        await _finish(client, worktree, record_id)
    store = GoalStore(worktree / ".booley_project")
    assert store.load(record_id).state is GoalState.FINISHED
    _assert_openroad(DevelopmentState.load(record_paths(store.project_dir, record_id).state_file))


def test_goal_mode_success_staleness_and_openroad(tmp_path: Path) -> None:
    asyncio.run(_success(_worktree(tmp_path)))


async def _failure(worktree: Path) -> None:
    async with Client(_server(worktree), mode="2026-07-28") as client:
        record_id = await _enter(client, worktree, [{"family": "sim", "target": "sim_fail"}])
        # Failed Flow evidence is expected; the MCP call itself must still return proof.
        response = await client.call_tool("sim", {"target": "sim_fail", "work_dir": str(worktree)})
        text = "\n".join(block.text for block in response.content if hasattr(block, "text"))
        assert "EXIT_CODE: 1" in text and "intentional Goal Mode smoke failure" in text
        status = await tool_text(client, "goal_status", {"work_dir": str(worktree)})
        assert status_rows(status, record_id, ["sim_pass_sim_fail"]) == {
            "sim_pass_sim_fail": "unmet"
        }
        before = _run_git(worktree, "rev-parse", "HEAD").stdout
        from goal_mode_driver import GoalDriverError

        with pytest.raises(GoalDriverError, match="met and fresh"):
            await _finish(client, worktree, record_id)
        assert _run_git(worktree, "rev-parse", "HEAD").stdout == before
        assert GoalStore(worktree / ".booley_project").load(record_id).state is GoalState.ACTIVE


def test_goal_mode_failing_sim_refuses_finish(tmp_path: Path) -> None:
    asyncio.run(_failure(_worktree(tmp_path)))
