"""Drive Goal Mode through modern stdio MCP in a disposable user-style workspace.

Readiness leaves the Goal Mode active in a disposable workspace. Caller inputs
are only cloned. The user worktree script creates the outer checkout, then this
driver replaces its Stealth Project copy with a paired linked Project worktree.
This workaround remains until ``booley worktree new`` creates that pairing itself.
The control Project is the main checkout's directory. Preview is explicitly
enabled here for 7a; 7b removes it.
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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from mcp import Client, StdioServerParameters

from booley.dev_support.demo_contract_codec import load_contract
from booley.goals.model import GoalArg, GoalFamily, GoalRecord, GoalState, goal_arg_to_json
from booley.goals.store import GoalStore
from booley.goals.translate import translate_goals
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.paths import worktree_create_script
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_prepare import prepare_project
from booley.runtime.project_repositories import is_git_worktree_root
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

MCP_PROTOCOL_VERSION = "2026-07-28"
TOOL_TIMEOUT_SECONDS = 600
_ENTRY = re.compile(r"^Goal Mode ([A-Za-z0-9-]+) entered in (.+)\.$", re.MULTILINE)
_EXIT = re.compile(r"EXIT_CODE:\s*(-?\d+)")
_RUN = re.compile(r"run_id=([^\s)]+)")


class GoalDriverError(RuntimeError):
    """A lifecycle step or its proof is absent."""


class GoalToolRefusalError(GoalDriverError):
    """An MCP response refuses the requested operation, including unflagged ERRORs."""


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class Workspace:
    primary: Path
    worktree: Path

    @property
    def project_dir(self) -> Path:
        """The main Project owns Goal state for every linked checkout."""
        return self.primary / ".booley_project"


async def tool_text(
    session: ToolSession, name: str, arguments: dict[str, Any], *, deadline: float | None = None
) -> str:
    """Classify tool refusals once and require proof within the remaining budget."""
    remaining = (
        TOOL_TIMEOUT_SECONDS if deadline is None else deadline - asyncio.get_running_loop().time()
    )
    try:
        result = await asyncio.wait_for(
            session.call_tool(name, arguments), timeout=max(0, remaining)
        )
    except TimeoutError as exc:
        raise GoalDriverError(f"{name} timed out") from exc
    text = "\n".join(
        block.text for block in result.content if isinstance(getattr(block, "text", None), str)
    )
    if getattr(result, "isError", False) or re.search(r"^\s*ERROR:", text, re.MULTILINE):
        raise GoalToolRefusalError(f"{name} refused: {text}")
    if not text.strip():
        raise GoalDriverError(f"{name} returned no proof")
    return text


async def run_flow(
    session: ToolSession,
    name: str,
    arguments: dict[str, Any],
    *,
    poll_limit: int = 20,
    expected_exit_code: int = 0,
) -> str:
    """Require exit zero under one absolute deadline, including detached Jobs."""
    deadline = asyncio.get_running_loop().time() + TOOL_TIMEOUT_SECONDS
    text = await tool_text(session, name, arguments, deadline=deadline)
    run = _RUN.search(text)
    for index in range(poll_limit + 1):
        exit_code = _EXIT.search(text)
        if exit_code is not None:
            if int(exit_code.group(1)) != expected_exit_code:
                raise GoalDriverError(f"{name} failed: {text}")
            return text
        if run is None:
            raise GoalDriverError(f"{name} returned neither EXIT_CODE nor run_id: {text}")
        if index == poll_limit:
            break
        text = await tool_text(
            session, "booley_poll", {"run_id": run.group(1), "wait_seconds": 30}, deadline=deadline
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


async def enter(session: ToolSession, worktree: Path, goals: Sequence[GoalArg]) -> dict[str, Any]:
    """Preserve entry warnings as evidence; only refusals or missing proof fail."""
    text = await tool_text(
        session,
        "goal_enter",
        {
            "work_dir": str(worktree),
            "slug": "ci-demo",
            "goals": [goal_arg_to_json(goal) for goal in goals],
        },
    )
    match = _ENTRY.search(text)
    if match is None or match.group(2) != str(worktree):
        raise GoalDriverError(f"goal_enter did not prove entry: {text}")
    warnings = _entry_warnings(text)
    return {"record_id": match.group(1), "status": "pass", "response": text, "warnings": warnings}


def _entry_warnings(text: str) -> list[str]:
    """Entry renders warning bullets before a blank line and the Rules block."""
    warnings = []
    for line in text.partition("Warnings:\n")[2].splitlines():
        if not line.startswith("- "):
            break
        warnings.append(line.removeprefix("- "))
    return warnings


async def _status(
    session: ToolSession, worktree: Path, entry: dict[str, Any], goals: Sequence[GoalArg]
) -> dict[str, Any]:
    text = await tool_text(session, "goal_status", {"work_dir": str(worktree)})
    keys = [goal.key for goal in translate_goals(goals).goals]
    rows = status_rows(text, entry["record_id"], keys)
    return {
        "record_id": entry["record_id"],
        "goals": rows,
        "state": "active",
        "steps": {"goal_enter": entry, "goal_status": {"status": "pass", "response": text}},
    }


async def readiness(
    session: ToolSession, worktree: Path, goals: Sequence[GoalArg]
) -> dict[str, Any]:
    """Enter the full contract and list every Goal without a lifecycle exception."""
    entry = await enter(session, worktree, goals)
    return await _status(session, worktree, entry, goals)


async def roundtrip(
    session: ToolSession, worktree: Path, goals: Sequence[GoalArg]
) -> dict[str, Any]:
    """A lint-only Goal must be met/fresh before a genuine Finish."""
    if len(goals) != 1 or goals[0].family is not GoalFamily.LINT:
        raise GoalDriverError("surface round trip requires exactly one lint Goal")
    entry = await enter(session, worktree, goals)
    lint = await run_flow(session, "lint", {"work_dir": str(worktree), "target": goals[0].target})
    result = await _status(session, worktree, entry, goals)
    if any(value != "met" for value in result["goals"].values()):
        raise GoalDriverError("lint Goal is not met and fresh")
    finished = await finish(session, worktree, entry["record_id"])
    result["steps"].update(
        lint={"status": "pass", "response": lint},
        goal_finish={"status": "pass", "response": finished},
    )
    result["state"] = "finished"
    return result


async def finish(session: ToolSession, worktree: Path, record_id: str) -> str:
    """Use the shared refusal classifier and require exact Finish proof."""
    response = await tool_text(
        session,
        "goal_finish",
        {
            "work_dir": str(worktree),
            "record_id": record_id,
            "operation_id": str(uuid4()),
            "summary": "CI exercised real Flows and fresh Goal evidence in an isolated clone.",
        },
    )
    if (
        re.search(rf"^Goal Mode {re.escape(record_id)} finished\.$", response, re.MULTILINE)
        is None
    ):
        raise GoalDriverError(f"goal_finish did not prove completion: {response}")
    return response


def _run(
    command: list[str], cwd: Path, env: dict[str, str], *, stdin_payload: str | None = None
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=stdin_payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode:
        raise GoalDriverError(f"command failed: {command!r}\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def create_workspace(
    source: Path, project_state: Path, directory: Path, python: Path = Path(sys.executable)
) -> Workspace:
    """Clone participants and invoke the normal user worktree command."""
    primary = directory / "primary"
    env = os.environ | {
        "BOOLEY_IN_SANDBOX": "1",
        "BOOLEY_PROJECT_DIR": str(primary / ".booley_project"),
        "PROJECT_ROOT": str(primary),
    }
    _run(["git", "clone", "--no-hardlinks", "--local", str(source), str(primary)], directory, env)
    paired = _clone_project(project_state, primary, directory, env)
    with (primary / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("\n/.booley_project\n/.booley-projected-*.core\n")
    _run(
        [bash_bin(), str(worktree_create_script())],
        primary,
        env | {"BOOLEY_PYTHON": str(python)},
        stdin_payload=json.dumps(
            {"name": "ci-demo", "cwd": str(primary), "on_existing": "refuse"}
        ),
    )
    worktree = primary / ".booley_project/worktrees/ci-demo"
    if paired:
        shutil.rmtree(worktree / ".booley_project")
        _run(
            ["git", "worktree", "add", "--detach", str(worktree / ".booley_project"), "HEAD"],
            primary / ".booley_project",
            env,
        )
    for repository in (primary, *((primary / ".booley_project",) if paired else ())):
        _run(["git", "config", "user.name", "Booley CI"], repository, env)
        _run(
            ["git", "config", "user.email", "booley-ci@users.noreply.github.com"], repository, env
        )
    return Workspace(primary, worktree)


def _clone_project(
    project_state: Path, primary: Path, directory: Path, env: dict[str, str]
) -> bool:
    paired = is_git_worktree_root(project_state)
    if paired:
        _run(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--local",
                str(project_state),
                str(primary / ".booley_project"),
            ],
            directory,
            env,
        )
    if not (primary / ".booley_project/booley.toml").is_file():
        raise GoalDriverError(
            "Project inputs must be versioned in the source or a Project repository"
        )
    return paired


@asynccontextmanager
async def client_session(
    workspace: Workspace, python: Path = Path(sys.executable)
) -> AsyncIterator[Client]:
    """The driver and image smoke share control paths, environment and protocol."""
    env = os.environ | {
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_GOAL_MODE_PREVIEW": "1",
        "BOOLEY_PROJECT_DIR": str(workspace.project_dir),
        "BOOLEY_IN_SANDBOX": "1",
    }
    server = StdioServerParameters(
        command=str(python), args=["-m", "booley.mcp.server"], cwd=str(workspace.worktree), env=env
    )
    async with Client(server, mode=MCP_PROTOCOL_VERSION) as client:
        if client.protocol_version != MCP_PROTOCOL_VERSION:
            raise GoalDriverError("MCP negotiated an unexpected protocol")
        yield client


async def _client_run(
    workspace: Workspace, goals: Sequence[GoalArg], python: Path, check_readiness: bool
) -> dict[str, Any]:
    async with client_session(workspace, python) as client:
        operation = readiness if check_readiness else roundtrip
        result = await operation(client, workspace.worktree, goals)
    record = GoalStore(workspace.project_dir).load(result["record_id"])
    expected = GoalState.ACTIVE if check_readiness else GoalState.FINISHED
    if record.state is not expected:
        raise GoalDriverError(f"persisted Goal state {record.state} differs from {expected}")
    if check_readiness:
        result["resolved_targets"] = _resolved_targets(record, workspace.worktree, goals)
    return {"schema": 1, "protocol": MCP_PROTOCOL_VERSION, "disposable": True, **result}


def _resolved_targets(
    record: GoalRecord, worktree: Path, goals: Sequence[GoalArg]
) -> dict[str, str]:
    """Prove recorded Goal bindings through the same Target catalog used at entry."""
    expected = {spec.key: spec.target for spec in translate_goals(goals).goals}
    actual = {goal.spec.key: goal.spec.target for goal in record.goals}
    if actual != expected:
        raise GoalDriverError("saved Goal Target bindings differ from the declared Goals")
    resolved: dict[str, str] = {}
    try:
        catalog = TargetCatalog.build(worktree)
        for key, target in actual.items():
            if target is None:
                raise GoalDriverError(f"Goal {key} has no Target")
            resolved[key] = catalog.select(target).identity
    except FuseSocError as exc:
        raise GoalDriverError(f"readiness Target resolution failed: {exc}") from exc
    return resolved


def validate(
    *,
    project: Path,
    project_state: Path,
    goals: Sequence[GoalArg],
    python: Path = Path(sys.executable),
    readiness: bool = False,
) -> dict[str, Any]:
    """Operate only on a clone, preserving caller Project inputs and Git state."""
    with tempfile.TemporaryDirectory(prefix="booley-ci-goals-") as directory:
        workspace = create_workspace(
            project.resolve(), project_state.resolve(), Path(directory), python
        )
        preparation = prepare_project(
            workspace.primary, workspace.worktree, slug="ci-demo", sim_flow_enabled=True
        )
        if not preparation.ok:
            raise GoalDriverError(preparation.error)
        return asyncio.run(_client_run(workspace, goals, python, readiness))


def write_evidence(path: Path, result: dict[str, Any]) -> None:
    """Publish JSON atomically so interrupted writes cannot masquerade as proof."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(path, (json.dumps(result, indent=2) + "\n").encode())


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
        goals = tuple(goal for goal in goals if goal.family is GoalFamily.LINT)
    result = validate(
        project=args.project,
        project_state=args.project_state,
        goals=goals,
        readiness=args.readiness,
    )
    write_evidence(args.evidence, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
