"""Drive Goal Mode through the modern stdio MCP wire in an isolated clone.

Readiness leaves the Goal Mode active in a disposable workspace. Every call
creates its own temporary clone and Project snapshot; no lifecycle operation
targets the caller's Project. The lint surface path requires a real Finish.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from mcp import Client, StdioServerParameters

from booley.dev_support.demo_contract_codec import load_contract
from booley.goals.model import GoalState, parse_goal_arg
from booley.goals.store import GoalStore
from booley.goals.translate import translate_goals
from booley.runtime.project_prepare import prepare_project

_ENTRY = re.compile(r"^Goal Mode ([A-Za-z0-9-]+) entered in (.+)\.$", re.MULTILINE)
_EXIT = re.compile(r"EXIT_CODE:\s*(-?\d+)")
_RUN = re.compile(r"run_id=([^\s)]+)")


class GoalDriverError(RuntimeError):
    """A required Goal lifecycle step or its proof is absent."""


class ToolSession(Protocol):
    """The SDK seam used by deterministic, Docker-free driver tests."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


async def tool_text(session: ToolSession, name: str, arguments: dict[str, Any]) -> str:
    """Require a nonempty successful MCP response within a bounded call."""
    result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=650)
    text = "\n".join(
        block.text for block in result.content if isinstance(getattr(block, "text", None), str)
    )
    if getattr(result, "isError", False) or not text.strip():
        raise GoalDriverError(f"{name} failed or returned no proof: {text}")
    return text


async def run_flow(
    session: ToolSession, name: str, arguments: dict[str, Any], *, poll_limit: int = 20
) -> str:
    """Require exit zero, including detached Jobs; never accept a mere submit."""
    async with asyncio.timeout(600):
        return await _flow_completion(session, name, arguments, poll_limit)


async def _flow_completion(
    session: ToolSession, name: str, arguments: dict[str, Any], poll_limit: int
) -> str:
    text = await tool_text(session, name, arguments)
    run = _RUN.search(text)
    for index in range(poll_limit + 1):
        exit_code = _EXIT.search(text)
        if exit_code is not None:
            if int(exit_code.group(1)) != 0:
                raise GoalDriverError(f"{name} failed: {text}")
            return text
        if run is None:
            raise GoalDriverError(f"{name} returned neither EXIT_CODE nor run_id: {text}")
        if index == poll_limit:
            break
        text = await tool_text(
            session, "booley_poll", {"run_id": run.group(1), "wait_seconds": 30}
        )
    raise GoalDriverError(f"{name} Job did not finish within {poll_limit} polls")


def status_rows(text: str, record_id: str, keys: Sequence[str]) -> dict[str, str]:
    """Require this active record and exactly the declared Goal status rows."""
    header = re.search(
        rf"^{re.escape(record_id)} \(active\) · (\d+)/(\d+) met$", text, re.MULTILINE
    )
    matches = re.findall(r"^\s*(\S+)\s+(met|unmet|stale)(?:\s|$)", text, re.MULTILINE)
    rows = dict(matches)
    if len(matches) != len(keys):
        raise GoalDriverError(f"goal_status has missing or duplicate rows: {text}")
    if header is None or int(header.group(2)) != len(keys) or set(rows) != set(keys):
        raise GoalDriverError(f"goal_status did not list every declared Goal: {text}")
    if int(header.group(1)) != sum(value == "met" for value in rows.values()):
        raise GoalDriverError(f"goal_status count disagrees with its rows: {text}")
    return rows


async def exercise(
    session: ToolSession, worktree: Path, goals: Sequence[dict[str, Any]], *, readiness: bool
) -> dict[str, Any]:
    """Enter and list all Goals; the surface path additionally lints and finishes."""
    specs = translate_goals(tuple(parse_goal_arg(goal) for goal in goals)).goals
    entered = await tool_text(
        session, "goal_enter", {"work_dir": str(worktree), "slug": "ci-demo", "goals": list(goals)}
    )
    match = _ENTRY.search(entered)
    if match is None or match.group(2) != str(worktree) or "Warnings:" in entered:
        raise GoalDriverError(f"goal_enter did not prove resolved Goals: {entered}")
    record_id = match.group(1)
    if not readiness:
        if len(goals) != 1 or goals[0]["family"] != "lint":
            raise GoalDriverError("surface round trip requires exactly one lint Goal")
        await run_flow(session, "lint", {"work_dir": str(worktree), "target": goals[0]["target"]})
    status = await tool_text(session, "goal_status", {"work_dir": str(worktree)})
    rows = status_rows(status, record_id, [spec.key for spec in specs])
    result: dict[str, Any] = {"record_id": record_id, "goals": rows, "state": "active"}
    if not readiness:
        if any(value != "met" for value in rows.values()):
            raise GoalDriverError(f"lint Goal is not met and fresh: {status}")
        await _finish(session, worktree, record_id)
        result["state"] = "finished"
    return result


async def _finish(session: ToolSession, worktree: Path, record_id: str) -> None:
    response = await tool_text(
        session,
        "goal_finish",
        {
            "work_dir": str(worktree),
            "record_id": record_id,
            "operation_id": str(uuid4()),
            "summary": "CI exercised Goal entry, real lint, fresh status and Finish in an isolated clone.",
        },
    )
    if (
        re.search(rf"^Goal Mode {re.escape(record_id)} finished\.$", response, re.MULTILINE)
        is None
    ):
        raise GoalDriverError(f"goal_finish did not prove completion: {response}")


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=300
    )
    if result.returncode:
        raise GoalDriverError(f"command failed: {command!r}\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def _workspace(source: Path, project_state: Path, directory: Path) -> Path:
    """Clone before mutation and copy only Project inputs into a linked worktree."""
    primary = directory / "primary"
    worktree = directory / "worktree"
    env = dict(os.environ)
    _run(["git", "clone", "--no-hardlinks", "--local", str(source), str(primary)], directory, env)
    _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], primary, env)
    # A nonversioned Project snapshot mirrors the linked-worktree live fixtures.
    shutil.copytree(
        project_state,
        worktree / ".booley_project",
        ignore=shutil.ignore_patterns(
            ".git", "tickets", "goals", "logs", ".runtime", "worktrees", "__pycache__"
        ),
    )
    with (primary / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("\n/.booley_project\n/.booley-projected-*.core\n")
    _run(["git", "config", "user.name", "Booley CI"], worktree, env)
    _run(["git", "config", "user.email", "booley-ci@users.noreply.github.com"], worktree, env)
    return worktree


async def _client_run(
    worktree: Path, goals: Sequence[dict[str, Any]], python: Path, readiness: bool
) -> dict[str, Any]:
    env = os.environ | {
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_GOAL_MODE_PREVIEW": "1",
        "BOOLEY_PROJECT_DIR": str(worktree / ".booley_project"),
        "BOOLEY_IN_SANDBOX": "1",
    }
    server = StdioServerParameters(
        command=str(python), args=["-m", "booley.mcp.server"], cwd=str(worktree), env=env
    )
    async with Client(server, mode="2026-07-28") as client:
        if client.protocol_version != "2026-07-28":
            raise GoalDriverError("MCP negotiated an unexpected protocol")
        result = await exercise(client, worktree, goals, readiness=readiness)
    record = GoalStore(worktree / ".booley_project").load(result["record_id"])
    expected = GoalState.ACTIVE if readiness else GoalState.FINISHED
    if record.state is not expected:
        raise GoalDriverError(f"persisted Goal state {record.state} differs from {expected}")
    return {"schema": 1, "protocol": "2026-07-28", "disposable": True, **result}


def validate(
    *,
    project: Path,
    project_state: Path,
    goals: Sequence[dict[str, Any]],
    python: Path = Path(sys.executable),
    readiness: bool = False,
) -> dict[str, Any]:
    """Run only inside a driver-owned disposable clone, preserving caller inputs."""
    with tempfile.TemporaryDirectory(prefix="booley-ci-goals-") as directory:
        worktree = _workspace(project.resolve(), project_state.resolve(), Path(directory))
        preparation = prepare_project(worktree, worktree, slug="ci-demo", sim_flow_enabled=True)
        if not preparation.ok:
            raise GoalDriverError(preparation.error)
        return asyncio.run(_client_run(worktree, goals, python, readiness))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--project-state", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    goals = load_contract(args.contract).required_goals
    if not args.readiness:
        goals = tuple(goal for goal in goals if goal["family"] == "lint")
    result = validate(
        project=args.project,
        project_state=args.project_state,
        goals=goals,
        readiness=args.readiness,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
