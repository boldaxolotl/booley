"""Test support: drive Goal Mode through modern stdio MCP in a disposable workspace.

Goal Mode's own tests and smoke use it; nothing in the release or demo checks
does. Caller inputs are only cloned. ``booley worktree new`` creates the outer checkout and its
Paired Checkout through the same path used in Interactive Mode.
The Main Booley Directory is the main checkout's directory. Preview is explicitly
enabled here for 7a; 7b removes it.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from mcp import Client, StdioServerParameters

from booley.goals.model import GoalArg, goal_arg_to_json
from booley.runtime.project_repositories import is_git_worktree_root

MCP_PROTOCOL_VERSION = "2026-07-28"
TOOL_TIMEOUT_SECONDS = 600
POLL_WAIT_SECONDS = 30
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
    def booley_dir(self) -> Path:
        """The main Project owns Goal state for every linked checkout."""
        return self.primary / ".booley"


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
    timeout_seconds: float = TOOL_TIMEOUT_SECONDS,
) -> str:
    """Require exit zero under one absolute deadline, including detached Jobs."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
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
            session,
            "booley_poll",
            {"run_id": run.group(1), "wait_seconds": POLL_WAIT_SECONDS},
            deadline=deadline,
        )
    raise GoalDriverError(f"{name} Job did not finish within {poll_limit} polls")


def status_rows(text: str, record_id: str, keys: Sequence[str]) -> dict[str, str]:
    """Require this active record and exactly the declared Goal status rows."""
    header = re.search(
        rf"^{re.escape(record_id)} \(active\) · (\d+)/(\d+) met$", text, re.MULTILINE
    )
    matches = re.findall(
        r"^\s*(\S+)(?:[ \t]+\([^\n]*\))?[ \t]+(met|unmet|stale)(?:\s|$)",
        text,
        re.MULTILINE,
    )
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
    # Kept on subprocess.run: the CI driver streams stdin to a CLI that run_bounded_child cannot feed.
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
        "BOOLEY_DIR": str(primary / ".booley"),
        "PROJECT_ROOT": str(primary),
    }
    _run(["git", "clone", "--no-hardlinks", "--local", str(source), str(primary)], directory, env)
    paired = _clone_project(project_state, primary, directory, env)
    with (primary / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("\n/.booley\n/.booley-projected-*.core\n")
    for repository in (primary, *((primary / ".booley",) if paired else ())):
        _run(["git", "config", "user.name", "Booley CI"], repository, env)
        _run(
            ["git", "config", "user.email", "booley-ci@users.noreply.github.com"], repository, env
        )
    _run(
        [str(python), "-m", "booley.harness.booley", "worktree", "new", "ci-demo"],
        primary,
        env,
    )
    worktree = primary / ".booley/worktrees/ci-demo"
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
                str(primary / ".booley"),
            ],
            directory,
            env,
        )
    if not (primary / ".booley/booley.toml").is_file():
        raise GoalDriverError(
            "Project inputs must be versioned in the source or a Versioned Booley Directory"
        )
    return paired


@asynccontextmanager
async def client_session(
    workspace: Workspace, python: Path = Path(sys.executable)
) -> AsyncIterator[Client]:
    """The driver and image smoke share control paths, environment and protocol."""
    env = os.environ | {
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_DIR": str(workspace.booley_dir),
        "BOOLEY_IN_SANDBOX": "1",
    }
    server = StdioServerParameters(
        command=str(python), args=["-m", "booley.mcp.server"], cwd=str(workspace.worktree), env=env
    )
    async with Client(server, mode=MCP_PROTOCOL_VERSION) as client:
        if client.protocol_version != MCP_PROTOCOL_VERSION:
            raise GoalDriverError("MCP negotiated an unexpected protocol")
        yield client
