"""Test fixtures for booley tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

# Stub claude_agent_sdk before any booley imports touch it
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
    _sdk.ProcessError = type("ProcessError", (Exception,), {})
    _sdk.RateLimitEvent = type("RateLimitEvent", (), {})
    _sdk.ResultMessage = type("ResultMessage", (), {})
    _sdk.UserMessage = type("UserMessage", (), {})
    _sdk.query = AsyncMock()
    sys.modules["claude_agent_sdk"] = _sdk

    # Stub _internal.transport.subprocess_cli for the node.exe monkey-patch
    _internal = ModuleType("claude_agent_sdk._internal")
    _transport = ModuleType("claude_agent_sdk._internal.transport")
    _subprocess_cli = ModuleType("claude_agent_sdk._internal.transport.subprocess_cli")
    _subprocess_cli.SubprocessCLITransport = type(
        "SubprocessCLITransport",
        (),
        {
            "_build_command": lambda self: [],
        },
    )
    _sdk._internal = _internal
    _internal.transport = _transport
    _transport.subprocess_cli = _subprocess_cli
    sys.modules["claude_agent_sdk._internal"] = _internal
    sys.modules["claude_agent_sdk._internal.transport"] = _transport
    sys.modules["claude_agent_sdk._internal.transport.subprocess_cli"] = _subprocess_cli

from booley.harness.models import (
    AgentResult,
    TicketContext,
)


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in harness tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley" / "project"))


TICKETS_REL = Path(".booley") / "project" / "tickets"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Create a minimal project structure for testing."""
    # Create tickets directory structure (canonical layout)
    tickets = tmp_path / TICKETS_REL
    for subdir in ["queue", "active", "blocked", "waiting", "archived", "review", "done"]:
        (tickets / "board" / subdir).mkdir(parents=True)
    (tickets / "logs").mkdir(parents=True)
    # Create .booley scripts dir
    (tmp_path / ".booley" / "src").mkdir(parents=True, exist_ok=True)
    # Create .git marker
    (tmp_path / ".git").write_text("gitdir: fake", encoding="utf-8")
    return tmp_path


@pytest.fixture
def sample_ticket(project_root: Path) -> Path:
    """Create a sample ticket .md file in queue/."""
    ticket_path = project_root / TICKETS_REL / "board" / "queue" / "fix-fsm-counter.md"
    ticket_path.write_text(
        "---\n"
        "summary: Fix FSM counter overflow\n"
        "type: bugfix\n"
        "branch: master\n"
        "scope:\n"
        "  - rtl/my_module.sv\n"
        "  - tb/my_module_tb.sv\n"
        "criteria:\n"
        "  mandatory:\n"
        "    sim_pass:\n"
        "      - tb/my_module_tb.sv @ config_a @ all @ fail -> pass\n"
        "      - tb/my_module_tb.sv @ config_b @ all @ pass -> pass\n"
        "on_success:\n"
        "  destination: review\n"
        "  merge: true\n"
        "  cleanup: true\n"
        "priority: high\n"
        "---\n"
        "## Bug Description\n"
        "FSM counter wraps at boundary.\n",
        encoding="utf-8",
    )
    return ticket_path


@pytest.fixture
def sample_ctx(project_root: Path) -> TicketContext:
    """Create a sample TicketContext."""
    return TicketContext(
        slug="fix-fsm-counter",
        ticket_path=project_root / TICKETS_REL / "board" / "queue" / "fix-fsm-counter.md",
        ticket_type="bugfix",
        branch="master",
        summary="Fix FSM counter overflow",
        scope_raw=["rtl/my_module.sv", "tb/my_module_tb.sv"],
        criteria={
            "mandatory": {
                "sim_pass": [
                    "tb/my_module_tb.sv @ config_a @ all @ fail -> pass",
                    "tb/my_module_tb.sv @ config_b @ all @ pass -> pass",
                ],
            },
        },
        priority="high",
        feature_branch="fix-fsm-counter",
        worktree_path=project_root / ".booley" / "worktrees" / "fix-fsm-counter",
        project_root=project_root,
    )


@pytest.fixture
def sample_exec_ctx() -> dict:
    """Sample execution context dict."""
    return {
        "configs": ["config_a", "config_b"],
        "defines": ["PROTECTED_MODE"],
        "testbench_top": "my_module_tb",
        "synthesis": {},
    }


@pytest.fixture
def sample_manifest() -> dict:
    """Sample command manifest dict (DEPRECATED -- for backward-compat tests)."""
    return {
        "sim_configs": {
            "config_a": {
                "run": {
                    "cmd": "python3 .booley/src/sim/run_sim_batch.py --config config_a --tb-top my_module_tb",
                    "timeout_ms": 600000,
                },
                "run_vcd": {
                    "cmd": "python3 .booley/src/sim/run_sim_batch.py --config config_a --tb-top my_module_tb --trace --debug",
                    "timeout_ms": 600000,
                },
            },
            "config_b": {
                "run": {
                    "cmd": "python3 .booley/src/sim/run_sim_batch.py --config config_b --tb-top my_module_tb",
                    "timeout_ms": 600000,
                },
                "run_vcd": {
                    "cmd": "python3 .booley/src/sim/run_sim_batch.py --config config_b --tb-top my_module_tb --trace --debug",
                    "timeout_ms": 600000,
                },
            },
        },
        "synthesis": {},
    }


@pytest.fixture
def mock_agent_result() -> AgentResult:
    """A mock agent result with some tokens."""
    return AgentResult(
        output="Done. Fixed the bug.",
        structured=None,
        input_tokens=5000,
        output_tokens=2000,
    )


def make_agent_result(
    output: str = "",
    structured: dict | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> AgentResult:
    """Helper to create AgentResult instances for tests."""
    return AgentResult(
        output=output,
        structured=structured,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# ---------------------------------------------------------------------------
# Shared test helpers (importable by test modules)
# ---------------------------------------------------------------------------


def setup_worktree(ctx: TicketContext) -> None:
    """Create minimal worktree dir structure expected by steps.

    Creates `.agents/plans` inside the work directory, a `.git` marker if
    worktree_path is set, and ensures `ctx.logs_dir` exists.
    """
    wt = ctx.work_dir
    (wt / ".agents" / "plans").mkdir(parents=True, exist_ok=True)
    if ctx.worktree_path:
        git_marker = ctx.worktree_path / ".git"
        if not git_marker.exists():
            git_marker.write_text("gitdir: fake", encoding="utf-8")
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
