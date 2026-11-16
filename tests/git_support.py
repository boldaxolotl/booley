"""Plain Git and Stealth fixtures shared by tests that build real repositories."""

from __future__ import annotations

from pathlib import Path

from tests.subprocess_budget import run_bounded_child

GIT_TIMEOUT_S = 30

CORE = """CAPI=2:
name: ::top:0
filesets:
  rtl: {files: [rtl.v], file_type: verilogSource}
  constraints:
    files: [.booley/cores/constraints/top.sdc]
    file_type: SDC
targets:
  top: {filesets: [rtl, constraints], toplevel: top, default_tool: verilator}
""".replace(" targets:", "targets:")


def git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and return its stripped output.

    Automatic maintenance is off: a detached repack started by one fixture
    commit would otherwise change the object store under a later assertion.
    """
    result = run_bounded_child(
        [
            "git",
            "-c",
            "user.name=Goal Test",
            "-c",
            "user.email=goal@test.invalid",
            "-c",
            "core.autocrlf=false",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *args,
        ],
        cwd=cwd,
        timeout=GIT_TIMEOUT_S,
        check=True,
    )
    return result.stdout.strip()


def hostile(foreign: Path) -> dict[str, str]:
    """Git environment variables that point every Git call at *foreign*."""
    return {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_COMMON_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git/index"),
        "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / "missing-objects"),
        "GIT_PREFIX": "wrong/",
        "GIT_CEILING_DIRECTORIES": str(foreign.parent),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
        "GIT_ATTR_SOURCE": "0" * 40,
    }


def repository(path: Path) -> Path:
    """A new empty Git repository at *path* on ``main``."""
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "core.autocrlf", "false")
    return path
