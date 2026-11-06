"""Drive Goal Mode through modern stdio MCP in a disposable user-style workspace.

Readiness leaves the Goal Mode active in a disposable workspace. Caller inputs
are only cloned. ``booley worktree new`` creates the outer checkout and its
Paired Checkout through the same path used in Interactive Mode.
The Main Booley Directory is the main checkout's directory. Preview is explicitly
enabled here for 7a; 7b removes it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from mcp import Client, StdioServerParameters

from booley.dev_support import mutation_lock
from booley.dev_support.demo_contract_codec import load_contract
from booley.fusesoc.core_projection import native_cores_ignored
from booley.goals.model import GoalArg, GoalFamily, GoalRecord, GoalState, goal_arg_to_json
from booley.goals.naming import resolve_spelling
from booley.goals.paths import record_paths
from booley.goals.presentation import goal_presentation
from booley.goals.sim_baseline_store import BASELINE_FAMILIES, baseline_jobs_dir
from booley.goals.store import GoalStore
from booley.goals.translate import translate_goals
from booley.runtime import job_records
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.pid import is_pid_alive
from booley.runtime.process_group import ProcessGroup, request_group_termination
from booley.runtime.project_prepare import prepare_project
from booley.runtime.project_repositories import is_git_worktree_root
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

MCP_PROTOCOL_VERSION = "2026-07-28"
TOOL_TIMEOUT_SECONDS = 600
# The mutation smoke builds and simulates the baseline and one variant while the
# Goal's detached baseline Jobs compete for the same host.
MUTATION_SMOKE_TIMEOUT_SECONDS = 1200
POLL_WAIT_SECONDS = 30
MUTATION_SMOKE_SUFFIX = " mutation-smoke"
# A ``//`` comment that tools read as a directive (or a macro line continuation)
# is not inert: extending it could change compilation, so the smoke skips it.
_TOOL_DIRECTIVE = re.compile(
    r"//\s*(synopsys|pragma|verilator|synthesis|translate|cadence|vcs|xcelium|slang|verible"
    r"|coverage|lint|iverilog|yosys)",
    re.IGNORECASE,
)
# Bound for detached baseline Jobs to unwind after cancellation.
BASELINE_STOP_SECONDS = 60
# A SIGTERM sent before the Job supervisor installs its handler is lost, so the
# request is repeated; a Job still alive after the escalation point is killed
# outright (the workspace is disposable, nothing it holds needs a clean unwind).
BASELINE_RESIGNAL_SECONDS = 5
BASELINE_KILL_FRACTION = 0.5
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


async def _status(
    session: ToolSession, worktree: Path, entry: dict[str, Any], goals: Sequence[GoalArg]
) -> dict[str, Any]:
    text = await tool_text(session, "goal_status", {"work_dir": str(worktree)})
    names = {
        goal.key: goal_presentation(resolve_spelling(goal, worktree)).name
        for goal in translate_goals(goals).goals
    }
    public_rows = status_rows(text, entry["record_id"], list(names.values()))
    rows = {key: public_rows[name] for key, name in names.items()}
    return {
        "record_id": entry["record_id"],
        "goals": rows,
        "state": "active",
        "steps": {"goal_enter": entry, "goal_status": {"status": "pass", "response": text}},
    }


async def readiness(
    session: ToolSession,
    worktree: Path,
    goals: Sequence[GoalArg],
    *,
    mutation_smoke_booley_dir: Path | None = None,
) -> dict[str, Any]:
    """Enter the full contract and list every Goal without a lifecycle exception.

    With *mutation_smoke_booley_dir* (the Main Booley Directory) set, also run
    one small ``mutation_tester`` call inside the Goal worktree.
    """
    entry = await enter(session, worktree, goals)
    result = await _status(session, worktree, entry, goals)
    if mutation_smoke_booley_dir is not None:
        smoke = await mutation_smoke(
            session, worktree, mutation_smoke_booley_dir, entry["record_id"], goals
        )
        result["steps"]["mutation_tester"] = smoke
    return result


def _is_inert_line_comment(line: str) -> bool:
    """Whether appending text to *line* cannot change what a tool builds."""
    return (
        line.lstrip().startswith("//")
        and not line.rstrip().endswith("\\")
        and not _TOOL_DIRECTIVE.match(line.lstrip())
    )


def _smoke_scope_and_proposal(worktree: Path, goals: Sequence[GoalArg]) -> tuple[str, str, Any]:
    """Return ``(target, scope file, spec dict)`` for one comment-only mutation.

    The proposal extends the first inert ``//`` line comment of the first RTL file of
    the contract's mutation Target. The variant therefore compiles and runs
    exactly like the baseline: the smoke can only fail when a variant does not
    build or yields no verdict, which is what it guards.
    """
    goal = next((goal for goal in goals if goal.family is GoalFamily.MUTATION), None)
    if goal is None or goal.target is None:
        raise GoalDriverError("mutation smoke needs a mutation Goal with a Target")
    if not native_cores_ignored(worktree):
        raise GoalDriverError(
            "mutation smoke needs a Project with ignore_native_cores: the smoke guards the "
            "private mirror path"
        )
    try:
        catalog = TargetCatalog.build(worktree)
        files = catalog.inspect(catalog.select(goal.target)).rtl_files
    except FuseSocError as exc:
        raise GoalDriverError(f"mutation smoke Target resolution failed: {exc}") from exc
    if not files:
        raise GoalDriverError(f"mutation smoke Target {goal.target} lists no RTL file")
    scope = files[0]
    for number, line in enumerate((worktree / scope).read_text("utf-8").splitlines(), start=1):
        if _is_inert_line_comment(line):
            spec = {
                "index": 1,
                "category": "comment",
                "file": scope,
                "line": number,
                "original_code": line,
                "mutated_code": line + MUTATION_SMOKE_SUFFIX,
                "detectability_argument": "comment-only change: the build must still succeed",
            }
            return goal.target, scope, spec
    raise GoalDriverError(f"mutation smoke found no // line comment in {scope}")


async def mutation_smoke(
    session: ToolSession,
    worktree: Path,
    booley_dir: Path,
    record_id: str,
    goals: Sequence[GoalArg],
) -> dict[str, Any]:
    """Build and run one variant in the Goal worktree through the private mirror.

    A schema-2 proposal lock is seeded in the Goal run's runtime directory
    (``BOOLEY_RUNTIME_DIR`` for Goal calls) so warm reuse skips the creator
    agent, which the CI container cannot reach. ``diagnostic`` keeps the
    one-variant run from recording the Goal's mutation criterion as met.
    """
    target, scope, spec = _smoke_scope_and_proposal(worktree, goals)
    runtime_dir = record_paths(booley_dir, record_id).runtime_dir
    mutation_lock.save_lock(
        mutation_lock.LockMeta(
            created_at=mutation_lock.now_iso(),
            scope=[scope],
            scope_hashes=mutation_lock.compute_scope_hashes([scope], worktree),
            count=1,
            mutations=[spec],
        ),
        runtime_dir,
    )
    response = await run_flow(
        session,
        "mutation_tester",
        {
            "work_dir": str(worktree),
            "target": target,
            "scope": scope,
            "count": 1,
            "min_detected": 0,
            "diagnostic": True,
        },
        poll_limit=MUTATION_SMOKE_TIMEOUT_SECONDS // POLL_WAIT_SECONDS,
        timeout_seconds=MUTATION_SMOKE_TIMEOUT_SECONDS,
    )
    mirror = mutation_lock.variant_build_dir(1, runtime_dir) / "core-mirror"
    if not mirror.is_dir():
        raise GoalDriverError(f"mutation smoke never built through the private mirror: {mirror}")
    return {"status": "pass", "response": response}


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


async def _client_run(
    workspace: Workspace,
    goals: Sequence[GoalArg],
    python: Path,
    check_readiness: bool,
    smoke_mutation: bool = False,
) -> dict[str, Any]:
    async with client_session(workspace, python) as client:
        if check_readiness:
            result = await readiness(
                client,
                workspace.worktree,
                goals,
                mutation_smoke_booley_dir=workspace.booley_dir if smoke_mutation else None,
            )
        else:
            result = await roundtrip(client, workspace.worktree, goals)
    record = GoalStore(workspace.booley_dir).load(result["record_id"])
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


def stop_baseline_jobs(booley_dir: Path, *, timeout_s: float = BASELINE_STOP_SECONDS) -> None:
    """Cancel detached report-only baseline Jobs before the workspace is deleted.

    Active baseline calls schedule Jobs, under every registered family's root,
    that outlive the MCP server. They write under the disposable Project, so
    deleting it while they run races their writes. A Job still inside its spawn grace (no PID yet) is awaited
    until it stamps a PID and can be signalled.
    """
    job_roots = [baseline_jobs_dir(booley_dir, family) for family in BASELINE_FAMILIES]
    started = time.monotonic()
    deadline = started + timeout_s
    kill_at = started + timeout_s * BASELINE_KILL_FRACTION
    last_signalled: dict[int, float] = {}
    while True:
        active = [
            record
            for jobs in job_roots
            for record in job_records.list_records(jobs)
            if job_records.is_active(record, is_pid_alive)
        ]
        if not active:
            return
        now = time.monotonic()
        for record in active:
            if record.pid is None:
                continue
            if now - last_signalled.get(record.pid, -BASELINE_RESIGNAL_SECONDS) < (
                BASELINE_RESIGNAL_SECONDS
            ):
                continue
            # Always ask politely first; only a Job that already saw SIGTERM is killed.
            escalate = record.pid in last_signalled and now >= kill_at
            last_signalled[record.pid] = now
            group = ProcessGroup(record.pid)
            if escalate:
                # The driver runs only in the Linux CI image, so killpg is available.
                with contextlib.suppress(OSError):
                    os.killpg(group.id, signal.SIGKILL)
            else:
                # The supervisor leads its own process group; SIGTERM unwinds it.
                request_group_termination(group)
        if now >= deadline:
            names = ", ".join(sorted(record.run_id for record in active))
            raise GoalDriverError(f"baseline Jobs still running after {timeout_s:g}s: {names}")
        time.sleep(0.2)


def validate(
    *,
    project: Path,
    project_state: Path,
    goals: Sequence[GoalArg],
    python: Path = Path(sys.executable),
    readiness: bool = False,
    mutation_smoke: bool = False,
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
        try:
            return asyncio.run(_client_run(workspace, goals, python, readiness, mutation_smoke))
        finally:
            stop_baseline_jobs(workspace.booley_dir)


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
    parser.add_argument(
        "--mutation-smoke",
        action="store_true",
        help="with --readiness, also run one small mutation_tester call in the Goal worktree",
    )
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.mutation_smoke and not args.readiness:
        parser.error("--mutation-smoke requires --readiness")
    goals = load_contract(args.contract).required_goals
    if not args.readiness:
        goals = tuple(goal for goal in goals if goal.family is GoalFamily.LINT)
    result = validate(
        project=args.project,
        project_state=args.project_state,
        goals=goals,
        readiness=args.readiness,
        mutation_smoke=args.mutation_smoke,
    )
    write_evidence(args.evidence, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
