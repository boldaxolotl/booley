"""Helpers for tests that kill or exit real child processes holding Booley scratch.

The children run a real nested Codex home and a real read-only Specialist
workspace so the tests prove cleanup against genuine process death, not a
simulated one. Every child is reaped before the helper returns.

The completed child fakes the Sandbox venue check.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child
from tests.time_budget import hang_guard_s

# Creates a nested Codex home and a read-only Specialist workspace, prints both
# paths, then SIGKILLs itself while the workspace is still open.
_KILLED_CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb
    from booley.specialists.specialist_workspace import isolated_agent_workspace

    nested = cb._ensure_nested_codex_home("killed", ["lint"])
    params = AgentCallParams(prompt="x", model="t", cwd=Path(sys.argv[1]))
    with isolated_agent_workspace(params, "read_only") as (isolated, _snapshot):
        print(nested, flush=True)
        print(isolated.cwd, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    """
)

# Runs the real ``_codex_spawn`` for several Specialist labels, then exits normally.
_COMPLETED_CHILD = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    cb._inside_container = lambda: True
    homes = []
    real_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        homes.append(kwargs["env"]["HOME"])
        return await real_spawn(sys.executable, "-c", "pass", **kwargs)

    asyncio.create_subprocess_exec = recording_spawn

    async def run():
        for label, tools in (("a", ["lint"]), ("b", ["sim"]), ("c", [])):
            params = AgentCallParams(
                prompt="x", model="t", cwd=Path(sys.argv[1]), label=label, nested_mcp_tools=tools
            )
            proc, _ = await cb._codex_spawn(["codex"], params)
            await proc.wait()

    asyncio.run(run())
    for home in homes:
        print(home, flush=True)
    """
)


def child_environment(scratch: Path, home: Path) -> dict[str, str]:
    """Return a child env whose temp directory and login home live in the test."""
    import booley

    source_root = str(Path(booley.__file__).resolve().parent.parent)
    env = {**os.environ, "TMPDIR": str(scratch), "HOME": str(home), "PYTHONPATH": source_root}
    env.pop("CODEX_HOME", None)
    return env


def run_child(script: str, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *script* in a child Python (reaped on return) and capture its output."""
    return run_bounded_child([sys.executable, "-c", script, str(repo)], env=env)


def kill_child_with_scratch(repo: Path, scratch: Path, home: Path) -> list[Path]:
    """SIGKILL a child holding a nested home and a workspace; return their top-level dirs."""
    result = run_child(_KILLED_CHILD, repo, child_environment(scratch, home))
    assert result.returncode == -signal.SIGKILL, result.stderr
    nested, workspace = (Path(line) for line in result.stdout.splitlines())
    # On unfixed code the nested home lives in /tmp, outside *scratch*.
    return [nested, scratch / workspace.relative_to(scratch).parts[0]]


def complete_runs(repo: Path, scratch: Path, home: Path) -> list[Path]:
    """Run several completed agent spawns in a child that exits normally."""
    result = run_child(_COMPLETED_CHILD, repo, child_environment(scratch, home))
    assert result.returncode == 0, result.stderr
    return [Path(line) for line in result.stdout.splitlines()]


def write_marker(directory: Path, owner: dict[str, object]) -> None:
    """Hand-write the owner marker format so tests do not depend on the writer."""
    marker = {"kind": "booley-owned-scratch", "version": 1, "owner": owner}
    (directory / ".booley-owner.json").write_text(json.dumps(marker), encoding="utf-8")


def exited_process_payload() -> dict[str, object]:
    """Return the identity payload of a real process that has already exited."""
    from booley.runtime.pid import capture_process_identity

    # Kept on Popen: needs a live child to capture its identity, then kills it.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = capture_process_identity(child.pid)
        assert identity is not None
    finally:
        child.kill()
        child.wait(timeout=SUBPROCESS_TIMEOUT_S)
    return identity.to_payload()


def make_repo(path: Path) -> Path:
    """Create a one-commit Git repository for the read-only workspace snapshot."""
    path.mkdir()
    (path / "tracked.sv").write_text("old\n", encoding="utf-8")
    for args in (
        ["init", "--quiet"],
        ["add", "tracked.sv"],
        ["-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "x"],
    ):
        run_bounded_child(["git", *args], cwd=path, check=True)
    return path


def run_together(*calls: Callable[[], Any]) -> list[Any]:
    """Run each call on its own thread, released together; return results in order."""
    start = threading.Barrier(len(calls))

    def run(call: Callable[[], Any]) -> Any:
        start.wait(timeout=hang_guard_s())
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def gate_marker_publishes(monkeypatch: Any, parties: int, deadline_s: float) -> None:
    """Hold each owner-marker publication until *parties* publishers have arrived.

    Orders the threads at the conflicting operation so a test cannot pass by the
    scheduler running the calls one after another. A caller that never gets
    company waits at most *deadline_s*, then proceeds.
    """
    barrier = threading.Barrier(parties)
    real_replace = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(dst).name == ".booley-owner.json":
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=deadline_s)
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
