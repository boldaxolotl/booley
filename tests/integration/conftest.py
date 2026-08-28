"""E2E test fixtures -- full isolation via temp project root.

Architecture:
- project_root = tmp dir (with .tickets/, .git init)
- ticket_board subprocesses use the real scripts dir (via patched _scripts_dir)
- TICKETS_DIR + PROJECT_ROOT env vars for subprocess isolation
- Worktrees created from real repo, referenced from mock project root
- Stage 01 (setup) bypassed with mock that uses worktree_factory
- All agent/sim/command boundaries mocked
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure package is importable & stub claude_agent_sdk
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

if "claude_agent_sdk" not in sys.modules:
    _sdk = ModuleType("claude_agent_sdk")
    _sdk.AssistantMessage = type("AssistantMessage", (), {})
    _sdk.ClaudeAgentOptions = type(
        "ClaudeAgentOptions",
        (),
        {
            "__init__": lambda self, **kw: self.__dict__.update(kw),
        },
    )
    _sdk.ClaudeSDKError = type("ClaudeSDKError", (Exception,), {})
    _sdk.ProcessError = type("ProcessError", (Exception,), {})
    _sdk.RateLimitEvent = type("RateLimitEvent", (), {})
    _sdk.ResultMessage = type("ResultMessage", (), {})
    _sdk.query = AsyncMock()
    sys.modules["claude_agent_sdk"] = _sdk

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end mock harness tests")


# ---------------------------------------------------------------------------
# Real project root (session scope, read-only)
# ---------------------------------------------------------------------------


def _find_real_root() -> Path | None:
    """Walk up from this file to find the project repo root.

    Skips .booley/ itself (which is a separate sub-repo with its own .git)
    by requiring the candidate NOT to be named '.booley'. Returns None if
    no host project is found (i.e. running framework tests standalone).
    """
    p = Path(__file__).resolve()
    while p.parent != p:
        if (
            p.name != ".booley"
            and (p / ".git").is_dir()
            and (
                (p / ".booley_project" / "tickets").is_dir()
                or (p / ".booley" / "project" / "tickets").is_dir()
            )
        ):
            return p
        p = p.parent
    return None


# Resolve lazily — fixture skips tests if no host project is available.
_REAL_ROOT = _find_real_root()


@pytest.fixture(scope="session")
def real_project_root() -> Path:
    if _REAL_ROOT is None:
        pytest.skip("no host project found; integration test requires embedding repo")
    return _REAL_ROOT


# ---------------------------------------------------------------------------
# Mock project root (per-test, fully isolated)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Create an isolated project root with .tickets/ and a git repo.

    This is the project_root passed to run_ticket(). All ticket state,
    checkpoints, and logs live here -- completely isolated from the real repo.
    """
    root = tmp_path / "project"
    root.mkdir()

    # Init git repo so any git commands in the developer work
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True, timeout=10)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "init"],
        capture_output=True,
        check=True,
        timeout=10,
    )

    # Create tickets tree (ticket_board uses board/ prefix for ticket dirs)
    tickets = root / ".booley" / "project" / "tickets"
    for subdir in ("queue", "active", "blocked", "waiting", "archived", "review", "done"):
        (tickets / "board" / subdir).mkdir(parents=True)
    (tickets / "logs").mkdir(parents=True)

    # Stub .booley/src (not used directly -- patched via _scripts_dir)
    (root / ".booley" / "src").mkdir(parents=True, exist_ok=True)

    return root


# ---------------------------------------------------------------------------
# Ticket creation helper
# ---------------------------------------------------------------------------


def _default_criteria(
    scope: list[str],
    ticket_type: str,
    sim_targets: list[str] | None = None,
    synthesis: bool = False,
) -> dict:
    """Build default criteria dict from scope files and ticket type."""
    sim_targets = sim_targets or ["config_a"]
    tb_files = [f for f in scope if f.startswith("tb/")]
    if not tb_files:
        tb_files = ["tb/my_module_tb.sv"]
    expectation = "fail" if ticket_type == "bugfix" else "pass"
    sim_entries = [
        f"{tb} @ {cfg} @ all @ {expectation} -> pass" for tb in tb_files for cfg in sim_targets
    ]
    mandatory: dict = {"sim_pass": sim_entries}
    criteria: dict = {"mandatory": mandatory}
    if synthesis:
        criteria.setdefault("optional", {})["synthesis_ok"] = {
            "cell_count_max": 500,
            "configs": sim_targets,
        }
    return criteria


def create_test_ticket(
    project_root: Path,
    slug: str,
    ticket_type: str = "bugfix",
    branch: str = "main",
    scope: list[str] | None = None,
    criteria: dict | None = None,
    synthesis: bool = False,
    sim_targets: list[str] | None = None,
    # Deprecated aliases (ignored)
    scope_current: list[str] | None = None,
    scope_new: list[str] | None = None,
    # Legacy params kept for signature compat but unused
    test: dict[str, str] | None = None,
    testbench: dict[str, str] | None = None,
    planned_stages: list[str] | None = None,
) -> Path:
    """Create a ticket .md file in queue/ and return the path.

    Also creates stub scope files so ticket_board validation passes.
    """
    scope = scope or scope_current or ["rtl/my_module.sv", "tb/my_module_tb.sv"]

    if criteria is None:
        criteria = _default_criteria(
            scope, ticket_type, sim_targets=sim_targets, synthesis=synthesis
        )

    # Create stub scope files so validate-ticket doesn't reject them
    for f in scope:
        clean = f.replace(" [new]", "")
        p = project_root / clean
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(f"// stub: {clean}\n", encoding="utf-8")
    # Also stub TB paths extracted from criteria
    from booley.dev_support.criteria import extract_tb_paths

    for tb_path in extract_tb_paths(criteria):
        p = project_root / tb_path
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(f"// stub: {tb_path}\n", encoding="utf-8")

    from booley.ticket_board.frontmatter import format_frontmatter

    fields = {
        "summary": f"E2E test ticket {slug}",
        "type": ticket_type,
        "branch": branch,
        "scope": scope,
        "criteria": criteria,
        "on_success": {"destination": "done", "merge": True, "cleanup": True},
        "priority": "high",
    }
    body = f"## Description\nE2E mock test: {slug}\n"
    content = format_frontmatter(fields, body)

    ticket_path = (
        project_root / ".booley" / "project" / "tickets" / "board" / "queue" / f"{slug}.md"
    )
    ticket_path.write_text(content, encoding="utf-8")
    return ticket_path


# ---------------------------------------------------------------------------
# Worktree factory
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree_factory(real_project_root: Path):
    """Callable that creates lightweight worktrees from the real repo."""
    from .worktree_helper import cleanup_worktree, create_lightweight_worktree

    created: list[str] = []

    def _factory(slug: str, base_branch: str = "main") -> Path:
        wt = create_lightweight_worktree(real_project_root, slug, base_branch)
        created.append(slug)
        return wt

    yield _factory

    for slug in created:
        with contextlib.suppress(Exception):
            cleanup_worktree(real_project_root, slug)


# ---------------------------------------------------------------------------
# Setup bypass
# ---------------------------------------------------------------------------


def make_setup_bypass(worktree_factory):
    """Return an async handler that replaces stage 01 (setup).

    Creates a lightweight worktree and populates ctx, skipping the
    real hook-based worktree creation (which needs DPI builds etc).
    Also freezes synthesis_baseline_sha (mirroring real stage 01).
    """
    from booley.harness.models import StepResult

    async def _mock_setup(ctx):
        wt = worktree_factory(ctx.slug, ctx.branch)
        ctx.worktree_path = wt
        ctx.feature_branch = ctx.slug

        return StepResult(
            metadata={"worktree": str(wt), "branch": ctx.slug},
        )

    return _mock_setup


# ---------------------------------------------------------------------------
# Preflight bypass (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_preflight():
    """Disable preflight checks for all E2E tests.

    Patches at the developer call site because developer.py does
    ``from .preflight import run_preflight`` at import time, which caches
    the reference in developer's namespace.  Patching the source module
    only works if developer hasn't been imported yet -- true in isolation
    but not when non-e2e tests have already primed the module cache.
    """
    with patch("booley.harness.developer.run_preflight"):
        yield


@pytest.fixture(autouse=True)
def _patch_project_configs():
    """No-op: the legacy project_config.CONFIGS registry was removed (ADR 0022
    decision 23 retired). Config selection now drives off ``.core`` Targets, and
    when none are authored ``resolve_target_selection`` passes raw config tokens
    through unvalidated — so e2e config names need no pre-registration."""
    yield


@pytest.fixture(autouse=True)
def _disable_docker_sandbox():
    """Disable Docker sandbox so E2E tests route through mocked ClaudeSDKBackend."""
    from booley.config.agent import (
        BackendConfig,
        SandboxConfig,
        set_backend_config,
    )
    from booley.runtime.agent_backend import ClaudeSDKBackend

    cfg = BackendConfig(
        active_backend=ClaudeSDKBackend(),
        sandbox=SandboxConfig(),
    )
    set_backend_config(cfg)
    yield
    set_backend_config(None)


# ---------------------------------------------------------------------------
# ticket_cli subprocess routing (use real scripts dir)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_scripts_dir():
    """No-op — ticket_cli now uses in-process DirectTicketOps (no subprocess).

    Kept as autouse fixture so tests that depend on fixture ordering aren't
    affected.
    """
    yield


@pytest.fixture(autouse=True)
def _env_isolation(project_root: Path):
    """Set env vars so ticket_board subprocesses use the isolated tickets dir."""
    old_td = os.environ.get("TICKETS_DIR")
    old_pr = os.environ.get("PROJECT_ROOT")
    old_nd = os.environ.get("NTFY_DISABLE")

    os.environ["TICKETS_DIR"] = str(project_root / ".booley" / "project" / "tickets")
    os.environ["PROJECT_ROOT"] = str(project_root)
    # Silence real ntfy.sh pushes: e2e tests drive state transitions that
    # would otherwise fire curl to the user's real topic.
    os.environ["NTFY_DISABLE"] = "1"

    yield

    # Restore
    for key, old in [("TICKETS_DIR", old_td), ("PROJECT_ROOT", old_pr), ("NTFY_DISABLE", old_nd)]:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
