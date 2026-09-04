"""Opt-in Ticket Mode boundary smoke against the production Session Runtime image."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from booley.core.models import AgentCallParams, AgentResult
from booley.criteria.state import DevelopmentState
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files
from booley.runtime import job_records, job_slots
from booley.runtime._codex_backend import CodexBackend
from booley.runtime.project_dir import reset_cache
from booley.ticket_board.contract_ops import open_contract, seal_contract
from booley.ticket_board.frontmatter import format_frontmatter

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOLEY_TICKET_MODE_SMOKE") != "1",
    reason="requires the production image, EDA toolchain, and /opt/pdk mount",
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ticket_mode_smoke"
_EXIT_CODE = re.compile(r"EXIT_CODE:\s*(-?\d+)")
_RUN_ID = re.compile(r"run_id=([^\s)]+)")
_OPTIONAL_KEY = "lint_clean_lint_optional"
_OPTIONAL_REASON = "Optional lint Target is intentionally left unmet to exercise reporting."


def _run_git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True, timeout=30
    )


def _initialize_project(tmp_path: Path) -> Path:
    project = tmp_path / "ticket-mode-project"
    shutil.copytree(_FIXTURE, project)
    project_dir = project / ".booley_project"
    board = project_dir / "tickets" / "board"
    for state in ("queue", "active", "blocked", "waiting", "archived", "review", "done"):
        (board / state).mkdir(parents=True)
    (project_dir / "tickets" / "logs").mkdir(parents=True)
    _run_git(project_dir, "init", "-b", "main")
    _run_git(project_dir, "config", "user.name", "Booley Smoke")
    _run_git(project_dir, "config", "user.email", "smoke@example.invalid")
    _run_git(project_dir, "add", ".")
    _run_git(project_dir, "commit", "-m", "Initialize Ticket Mode project data")
    _run_git(project, "init", "-b", "main")
    _run_git(project, "config", "user.name", "Booley Smoke")
    _run_git(project, "config", "user.email", "smoke@example.invalid")
    git_exclude = project / ".git" / "info" / "exclude"
    with git_exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n/.booley_project/\n")
    _run_git(project, "add", ".")
    _run_git(project, "commit", "-m", "Initialize Ticket Mode smoke fixture")
    reset_cache()
    return project


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
    assert "RESULT: WARN" in output
    report = json.loads((report_dir / "synth_synth.json").read_text(encoding="utf-8"))
    assert report["whs_ns"] >= 0
    log = Path(report["artifacts"]["log"])
    if not log.is_absolute():
        log = project / log
    assert "STA-0441" not in log.resolve().read_text(encoding="utf-8")


def _write_ticket(project: Path, slug: str, criteria: dict[str, Any], scope: list[str]) -> None:
    fields = {
        "summary": f"Production-image smoke for {slug}",
        "type": "verification",
        "branch": "main",
        "scope": scope,
        "criteria": criteria,
        "on_success": {
            "destination": "review",
            "merge": False,
            "cleanup": False,
            "triage_report": False,
        },
        "priority": "high",
    }
    content = format_frontmatter(fields, "## Description\nExercise real Ticket Mode boundaries.\n")
    queue = project / ".booley_project" / "tickets" / "board" / "queue"
    ticket = queue / f"{slug}.md"
    ticket.write_text(content, encoding="utf-8")
    open_contract(project, ticket, slug)
    seal_contract(project, ticket, slug)


def _success_criteria() -> dict[str, Any]:
    return {
        "mandatory": {
            "lint_clean": ["lint_smoke"],
            "elab_pass": ["sim_smoke"],
            "sim_pass": ["tb/tb_dut.sv @ sim_smoke @ all @ none -> pass"],
            "synthesis_ok": {
                "targets": ["synth_smoke"],
                "cell_count_max": 500,
                "clk_i.fmax_mhz_min": 1,
            },
        },
        "optional": {"lint_clean": ["lint_optional"]},
    }


def _blocked_criteria() -> dict[str, Any]:
    return {
        "mandatory": {
            "sim_pass": ["tb/tb_fail.sv @ sim_fail @ all @ none -> pass"],
        }
    }


def _tool_text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(getattr(block, "text", None), str)
    )


def _exit_code(text: str) -> int | None:
    match = _EXIT_CODE.search(text)
    return int(match.group(1)) if match else None


class McpDriver:
    """One real stdio MCP session with bounded detached-Job polling."""

    def __init__(self, session: Client) -> None:
        self.session = session
        self.calls: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[int, str]:
        self.calls.append(name)
        text = _tool_text(await self.session.call_tool(name, arguments))
        code = _exit_code(text)
        if code is not None:
            return code, text
        match = _RUN_ID.search(text)
        assert match, f"{name} returned neither EXIT_CODE nor run_id:\n{text}"
        return await self._poll(match.group(1), name)

    async def _poll(self, run_id: str, endpoint: str) -> tuple[int, str]:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            result = await self.session.call_tool(
                "booley_poll", {"run_id": run_id, "wait_seconds": 60}
            )
            text = _tool_text(result)
            code = _exit_code(text)
            if code is not None:
                return code, text
        pytest.fail(f"{endpoint} Job {run_id} did not finish within 600 seconds")


def _report_args(optional_reason: str | None = None) -> dict[str, str]:
    args = {
        "summary": "Exercised the production-image Ticket Mode smoke Project.",
        "uncertainties": "This is a deliberately tiny design, not representative QoR coverage.",
        "type_specific_detail": "Covered real Flow, MCP, Criteria, and Ticket Board behavior.",
    }
    if optional_reason is not None:
        args["optional_criteria_justification"] = optional_reason
    return args


def _load_state() -> DevelopmentState:
    return DevelopmentState.load(Path(os.environ["BOOLEY_STATE_FILE"]))


def _criterion(state: DevelopmentState, prefix: str) -> Any:
    matches = [value for key, value in state.criteria.items() if key.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix!r} criterion, got {len(matches)}"
    return matches[0]


async def _success_script(driver: McpDriver, observations: dict[str, Any]) -> None:
    for endpoint, arguments in (
        ("lint", {"target": "lint_smoke"}),
        ("sim", {"target": "sim_smoke", "elab_only": True}),
        ("sim", {"target": "sim_smoke"}),
        ("synth", {"target": "synth_smoke"}),
    ):
        code, text = await driver.call(endpoint, arguments)
        assert code == 0, f"{endpoint} failed:\n{text}"
    code, text = await driver.call("submit_run_report", _report_args())
    assert code == 2 and "optional criteria remain unmet" in text
    testbench = Path(observations["worktree"]) / "tb" / "tb_dut.sv"
    with testbench.open("a", encoding="utf-8") as stream:
        stream.write("\n// freshness probe\n")
    _run_git(testbench.parents[1], "add", "tb/tb_dut.sv")
    _run_git(testbench.parents[1], "commit", "-m", "test: add freshness probe")
    code, text = await driver.call("submit_run_report", _report_args(_OPTIONAL_REASON))
    assert code == 2 and "Newly stale" in text
    state = _load_state()
    observations["freshness"] = {key: value.met for key, value in state.criteria.items()}
    for arguments in (
        {"target": "sim_smoke", "elab_only": True},
        {"target": "sim_smoke"},
    ):
        code, text = await driver.call("sim", arguments)
        assert code == 0, f"sim rerun failed:\n{text}"
    code, text = await driver.call("submit_run_report", _report_args(_OPTIONAL_REASON))
    assert code == 0, text


async def _blocked_script(driver: McpDriver, observations: dict[str, Any]) -> None:
    code, text = await driver.call("sim", {"target": "sim_fail"})
    assert code == 1 and "intentional Ticket Mode smoke failure" in text
    code, text = await driver.call("submit_run_report", _report_args())
    assert code == 2 and "mandatory criteria remain unmet" in text
    state = _load_state()
    sim_key = next(key for key in state.criteria if key.startswith("sim_pass_"))
    observations["blocked_reason"] = f"Developer Agent exited with 1 unmet criteria: {sim_key}"


async def _run_mcp_script(
    params: AgentCallParams,
    script: Callable[[McpDriver, dict[str, Any]], Awaitable[None]],
    observations: dict[str, Any],
) -> AgentResult:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "booley.mcp.server"],
        cwd=str(params.cwd),
        env=dict(os.environ),
    )
    observations["worktree"] = str(params.cwd)
    async with Client(server, mode="2026-07-28") as client:
        assert client.protocol_version == "2026-07-28"
        observations["mcp_protocol_version"] = client.protocol_version
        driver = McpDriver(client)
        await script(driver, observations)
        observations["calls"] = list(driver.calls)
    return AgentResult(output="scripted Developer Agent completed")


def _blocked_diagnosis(reason: str) -> dict[str, Any]:
    return {
        "classification": "ticket-code",
        "board_reason": reason,
        "blocked_stage": "developer",
        "blockers": [
            {
                "name": "sim_pass",
                "reason": "The intentional failing simulation did not meet its Criterion.",
                "evidence": "The sim Flow report contains the expected failure sentinel.",
            }
        ],
        "passing_non_blocking": [],
        "developer_questions": [],
        "recommended_action": "Keep blocked; this is the expected smoke-test outcome.",
        "findings": [],
    }


def _install_scripted_backend(
    monkeypatch: pytest.MonkeyPatch,
    script: Callable[[McpDriver, dict[str, Any]], Awaitable[None]],
    observations: dict[str, Any],
) -> None:
    async def scripted_call(
        _backend: CodexBackend, params: AgentCallParams, **_kwargs: Any
    ) -> AgentResult:
        observations.setdefault("labels", []).append(params.label)
        if params.label == "developer":
            return await _run_mcp_script(params, script, observations)
        if params.label == "blocked-triage-report" and script is _blocked_script:
            return AgentResult(structured=_blocked_diagnosis(observations["blocked_reason"]))
        raise AssertionError(f"unexpected backend call: {params.label}")

    monkeypatch.setattr(CodexBackend, "call", scripted_call)


def _run_through_runner(monkeypatch: pytest.MonkeyPatch, project: Path, slug: str) -> int:
    from booley.harness import __main__ as harness_main
    from booley.harness import booley as runner

    def in_process_child(cmd: list[str], _cwd: str, child_project: Path) -> int:
        ticket = cmd[cmd.index("--ticket") + 1]
        args = argparse.Namespace(ticket=ticket, no_transcripts=True)
        return harness_main._run_harness(args, Path(child_project), use_console=False)

    monkeypatch.setattr(runner, "_run_with_heartbeat", in_process_child)
    args = argparse.Namespace(slug=slug, no_console=True, verbose=False)
    return runner._run_harness(args, project, sys.executable)[0]


def _prepare_environment(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    from booley.config import agent as agent_config

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project / ".booley_project"))
    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", "0")
    monkeypatch.setenv("BOOLEY_MCP_JOB_POLL_WAIT_SECONDS", "60")
    monkeypatch.setattr(agent_config, "_backend_config", None)
    reset_cache()


def _assert_installed_runner_guard(project: Path, slug: str) -> None:
    result = subprocess.run(
        ["booley", "run", "--project-root", str(project), "--ticket", slug, "--dry-run"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "[dry-run]" in output, output


def _assert_no_live_jobs() -> None:
    assert all(
        record.status != job_records.STATUS_RUNNING for record in job_records.list_records()
    )
    slots_root = job_slots.slots_dir()
    assert slots_root is not None
    holders, waiters = job_slots.SlotStore(slots_root).snapshot(job_slots.CLASS_TICKET)
    assert not holders and not waiters


def _assert_retained_worktree(project: Path, slug: str, expected_diff: list[str]) -> None:
    worktree = project / ".booley_project" / "worktrees" / slug
    assert worktree.is_dir()
    assert _run_git(worktree, "status", "--porcelain").stdout == ""
    changed = _run_git(project, "diff", "--name-only", f"main...{slug}").stdout.splitlines()
    assert changed == expected_diff
    assert _run_git(project, "branch", "--list", slug).stdout.strip().endswith(slug)


def _assert_board_state(project: Path, slug: str, expected: str) -> None:
    board = project / ".booley_project" / "tickets" / "board"
    assert (board / expected / f"{slug}.md").is_file()
    for state in ("queue", "active", "blocked", "review", "done"):
        if state != expected:
            assert not (board / state / f"{slug}.md").exists()


def _assert_openroad_criterion(state: DevelopmentState) -> None:
    entry = state.criteria["synthesis_ok_synth_smoke"]
    detail = entry.detail
    assert entry.met and detail["synth_mode"] == "physical"
    assert detail["area_source"] == "openroad_post_optimization"
    assert detail["ppa_complete"] is True and detail["timing_complete"] is True
    assert isinstance(detail["cells"], int) and detail["cells"] > 0
    clock = detail["per_clock"]["clk_i"]
    assert isinstance(clock["critical_path_ps"], (int, float))
    assert isinstance(clock["fmax_mhz"], (int, float))
    checks = {check["param"]: check for check in detail["checks"]}
    for parameter in ("cell_count_max", "clk_i.fmax_mhz_min"):
        assert checks[parameter]["pass"] is True
        assert checks[parameter].get("skipped") is not True
    assert not detail.get("infra_error")


def _logs_dir(project: Path, slug: str) -> Path:
    return project / ".booley_project" / "tickets" / "logs" / slug


def test_ticket_mode_success_staleness_optional_and_openroad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _initialize_project(tmp_path)
    slug = "ticket-mode-success"
    _write_ticket(project, slug, _success_criteria(), ["rtl/dut.sv", "tb/tb_dut.sv"])
    _prepare_environment(monkeypatch, project)
    _assert_installed_runner_guard(project, slug)
    observations: dict[str, Any] = {}
    _install_scripted_backend(monkeypatch, _success_script, observations)

    assert _run_through_runner(monkeypatch, project, slug) == 0

    _assert_board_state(project, slug, "review")
    state = DevelopmentState.load(_logs_dir(project, slug) / ".runtime" / "booley_state.json")
    assert all(entry.met for entry in state.criteria.values() if entry.mandatory)
    assert not state.criteria[_OPTIONAL_KEY].met
    assert state.criteria["_report_submitted"].detail["unmet_optional_criteria"] == [_OPTIONAL_KEY]
    _assert_openroad_criterion(state)
    stale = observations["freshness"]
    assert stale["lint_clean_lint_smoke"] and stale["synthesis_ok_synth_smoke"]
    assert not stale["elab_pass_sim_smoke"]
    assert not next(value for key, value in stale.items() if key.startswith("sim_pass_"))
    assert observations["calls"].count("synth") == 1
    assert observations["calls"].count("sim") == 4
    assert observations["labels"] == ["developer"]
    report = (_logs_dir(project, slug) / "REPORT.md").read_text(encoding="utf-8")
    assert _OPTIONAL_KEY in report and _OPTIONAL_REASON in report
    _assert_retained_worktree(project, slug, ["tb/tb_dut.sv"])
    _assert_no_live_jobs()


def test_ticket_mode_mandatory_sim_failure_moves_ticket_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _initialize_project(tmp_path)
    slug = "ticket-mode-blocked"
    _write_ticket(project, slug, _blocked_criteria(), ["rtl/dut.sv", "tb/tb_fail.sv"])
    _prepare_environment(monkeypatch, project)
    observations: dict[str, Any] = {}
    _install_scripted_backend(monkeypatch, _blocked_script, observations)

    assert _run_through_runner(monkeypatch, project, slug) == 0

    _assert_board_state(project, slug, "blocked")
    state = DevelopmentState.load(_logs_dir(project, slug) / ".runtime" / "booley_state.json")
    sim_entry = _criterion(state, "sim_pass_")
    assert not sim_entry.met and sim_entry.ever_failed
    assert not state.criteria["_report_submitted"].met
    assert not (_logs_dir(project, slug) / "REPORT.md").exists()
    assert observations["labels"] == ["developer", "blocked-triage-report"]
    assert observations["blocked_reason"] in (_logs_dir(project, slug) / "blocked.md").read_text(
        encoding="utf-8"
    )
    manifest = _logs_dir(project, slug) / ".runtime" / "triage-prep" / "blocked-manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "ready"
    _assert_retained_worktree(project, slug, [])
    _assert_no_live_jobs()
