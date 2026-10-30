"""Helpers for tests that kill or exit real child processes holding Booley scratch.

The children run a real nested Codex home and a real read-only Specialist
workspace so the tests prove cleanup against genuine process death, not a
simulated one. Every child is reaped before the helper returns.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

CHILD_TIMEOUT_S = 60

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
    return subprocess.run(
        [sys.executable, "-c", script, str(repo)],
        env=env,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )


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

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = capture_process_identity(child.pid)
        assert identity is not None
    finally:
        child.kill()
        child.wait(timeout=CHILD_TIMEOUT_S)
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
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path
