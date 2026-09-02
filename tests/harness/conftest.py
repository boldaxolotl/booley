"""Test fixtures for booley tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest


class _ReleaseImageDocker:
    """Configurable Docker boundary adapter for release-image tests."""

    def __init__(
        self,
        *,
        fingerprints: dict[str, str | None],
        oci_versions: dict[str, str | None] | None = None,
        pull_returncode: int = 0,
    ) -> None:
        self.fingerprints = fingerprints
        self.oci_versions = oci_versions or {}
        self.pull_returncode = pull_returncode
        self.commands: list[list[str]] = []

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"] and len(command) == 4:
            return subprocess.CompletedProcess(
                command,
                0 if command[3] in self.fingerprints else 1,
                "",
                "",
            )
        if command[:3] == ["docker", "image", "inspect"]:
            image = command[-1]
            is_fingerprint = any("booley.build-fingerprint" in part for part in command)
            labels = self.fingerprints if is_fingerprint else self.oci_versions
            value = labels.get(image)
            return subprocess.CompletedProcess(command, 0, f"{value or '<no value>'}\n", "")
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, self.pull_returncode, "", "not found")
        if command[:3] == ["docker", "system", "df"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def release_image_docker() -> type[_ReleaseImageDocker]:
    """Build a Docker adapter with per-image release provenance."""
    return _ReleaseImageDocker


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
    _sdk.ClaudeSDKError = type("ClaudeSDKError", (Exception,), {})
    _sdk.ProcessError = type("ProcessError", (Exception,), {})
    _sdk.RateLimitEvent = type("RateLimitEvent", (), {})
    _sdk.ResultMessage = type(
        "ResultMessage",
        (),
        {
            "__init__": lambda self, **kw: self.__dict__.update(kw),
        },
    )
    _sdk.UserMessage = type("UserMessage", (), {})
    _sdk.query = AsyncMock()
    sys.modules["claude_agent_sdk"] = _sdk

from booley.harness.models import TicketContext


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in harness tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
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
