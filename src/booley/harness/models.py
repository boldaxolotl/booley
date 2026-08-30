"""Data models for the Booley developer.

Development-specific types (``TicketContext``, ``ExecutionContext``, …) are
defined here. Dependency-free types shared with lower layers (``OnSuccess``,
``AgentCallParams``, ``AgentResult``) live in :mod:`booley.core.models` and are
re-exported below so existing ``from booley.harness.models import X`` callers
keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Re-exported from the core layer for backward compatibility. New cross-layer
# callers (Flows, MCP tools, ticket_board) should import these from booley.core.models
# directly to avoid depending upward on harness/.
from booley.core.models import (  # noqa: F401
    AgentCallParams,
    AgentResult,
    OnSuccess,
)
from booley.ticket_board.target_contract import TargetContract


@dataclass
class TicketContext:
    """Parsed ticket state -- populated from ticket_board.py parse-ticket + resume."""

    slug: str
    ticket_path: Path
    ticket_type: str  # feature | bugfix | refactor | verification
    branch: str  # base branch to merge into
    summary: str
    scope_raw: list[str] = field(
        default_factory=list
    )  # from YAML; entries may have " [new]" suffix
    spec: str = ""  # path to arch spec
    on_success: OnSuccess = field(default_factory=OnSuccess)
    dependencies: list[str] = field(default_factory=list)
    priority: str = "medium"
    # Criteria: single source of truth for what the harness must achieve
    criteria: dict[str, Any] = field(default_factory=dict)
    # Runtime state (populated by stage 0/1)
    feature_branch: str = ""
    worktree_path: Path | None = None
    completed_steps: list[str] = field(default_factory=list)
    current_step: str = ""
    workspace_intent: str = "fresh"  # fresh | resume
    # Project root (main repo, not worktree)
    project_root: Path = field(default_factory=Path.cwd)
    # Transcript logging (--no-transcripts to disable)
    save_transcripts: bool = True
    # Immutable outer-repository baseline stamped into ticket frontmatter.
    # Appended for positional compatibility with existing context constructors.
    base_sha: str = ""
    # Generation stamped atomically when this harness execution activates the ticket.
    execution_id: str = ""
    # Sealed Target/control-plane identity; appended for positional compatibility.
    target_contract: TargetContract | None = None
    # Intake defers sealed criteria state until the contract checkout is ready.
    criteria_state_needs_init: bool = False

    @property
    def work_dir(self) -> Path:
        """Working directory: worktree if available, else project root."""
        return self.worktree_path or self.project_root

    def sealed_contract_fields(self) -> dict[str, Any]:
        """Return the complete Ticket projection used for contract validation."""
        contract = self.target_contract
        if contract is None:
            raise ValueError("Ticket has no sealed Target contract")
        return {
            "base_sha": self.base_sha,
            "target_contract": contract.as_dict(),
            "criteria": self.criteria,
            "scope": self.scope_raw,
            "on_success": {
                "destination": self.on_success.destination,
                "merge": self.on_success.merge,
                "cleanup": self.on_success.cleanup,
                "triage_report": self.on_success.triage_report,
                "remove_targets": list(self.on_success.remove_targets),
            },
        }

    @staticmethod
    def _strip_new_tag(entry: str) -> str:
        return entry.removesuffix(" [new]")

    @property
    def scope(self) -> list[str]:
        """All scope paths with [new] tags stripped."""
        return [self._strip_new_tag(e) for e in self.scope_raw]

    @property
    def sim_targets(self) -> list[str]:
        """Derive unique targets from structured sim criteria entries."""
        from booley.dev_support.criteria import extract_sim_targets

        return extract_sim_targets(self.criteria)

    @property
    def has_synth(self) -> bool:
        """Whether criteria include any synthesis-related entries."""
        from booley.dev_support.criteria import has_synth_criteria

        return has_synth_criteria(self.criteria)

    @property
    def _tickets_dir(self) -> Path:
        """Resolved tickets directory (honors TICKETS_DIR env var)."""
        from booley.ticket_board.helpers import tickets_dir_from_project_root

        return tickets_dir_from_project_root(self.project_root)

    @property
    def logs_dir(self) -> Path:
        """Absolute path to ticket logs in MAIN repo."""
        return self._tickets_dir / "logs" / self.slug

    @property
    def is_integration(self) -> bool:
        """Integration ticket: branch starts with 'int/'."""
        return self.branch.startswith("int/")


@dataclass
class StepResult:
    """Result returned by a step handler."""

    metadata: dict[str, Any] = field(default_factory=dict)
    block_reason: str | None = None


@dataclass
class CommandEntry:
    """Single CLI command with timeout."""

    cmd: str
    timeout_ms: int = 600000

    @classmethod
    def from_dict(cls, d: dict, default_timeout: int = 600000) -> CommandEntry:
        return cls(cmd=d["cmd"], timeout_ms=d.get("timeout_ms", default_timeout))

    def to_dict(self) -> dict:
        return {"cmd": self.cmd, "timeout_ms": self.timeout_ms}


@dataclass
class ExecutionContext:
    """Resolved execution metadata for downstream stages.

    Agent stages compose their own CLI commands from these fields.
    Only synthesis retains pre-resolved CommandEntry objects (its stage
    is a dumb executor with no agent intelligence).
    """

    targets: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    testbench_top: str = ""
    top_level_tb: str = ""  # from booley.toml [sources.testbench]
    # EDA-tool backend names, all interpreted on the Edalize path (no harness
    # backend registry): `sim_eda_tool`/`lint_eda_tool` by the sim/lint Flows,
    # `synth_eda_tool` by the synth Flow.
    sim_eda_tool: str = "verilator-simple-sandbox"
    synth_eda_tool: str = "yosys-simple-sandbox"
    lint_eda_tool: str = "verilator-simple-sandbox"
    synthesis: dict[str, CommandEntry] = field(default_factory=dict)  # validated commands
    # Free-form command list authored in the run config by the ticket author.
    # Each entry is
    # {"cmd": "...", "purpose": "..."}.  Rendered in the execution context
    # block so agents get exact commands instead of guessing CLI flags.
    commands: list[dict[str, str]] = field(default_factory=list)
    # Frozen at stage 01 to ctx.branch tip; stage 10 synthesizes it as the
    # per-target area baseline (anchors the ±N% gate to "what we diverged from").
    synthesis_baseline_sha: str = ""
    # Preferred target for RTL mutation testing (most comprehensive).  Populated
    # by the run config; falls back to first available target if empty.
    mutation_testing_target: str = ""
    # Sentinel strings scanned in sim output to determine pass/fail.
    # Empty lists = no sentinel parsing, fall back to exit code.
    pass_sentinels: list[str] = field(default_factory=lambda: ["[SIM_RESULT] PASSED"])
    fail_sentinels: list[str] = field(default_factory=lambda: ["[SIM_RESULT] FAILED"])

    @staticmethod
    def _normalize_defines(defines: list[str]) -> list[str]:
        """Strip whitespace around colons in define strings."""
        return [d.replace(": ", ":").replace(" :", ":") if ":" in d else d for d in defines]

    @classmethod
    def from_dict(cls, d: dict) -> ExecutionContext:
        """Parse from JSON dict."""
        return cls(
            targets=d.get("targets", []),
            defines=cls._normalize_defines(d.get("defines", [])),
            parameters=dict(d.get("parameters", {})),
            testbench_top=d.get("testbench_top", ""),
            top_level_tb=d.get("top_level_tb", ""),
            sim_eda_tool=d.get("sim_eda_tool", "verilator-simple-sandbox"),
            synth_eda_tool=d.get("synth_eda_tool", "yosys-simple-sandbox"),
            lint_eda_tool=d.get("lint_eda_tool", "verilator-simple-sandbox"),
            synthesis={n: CommandEntry.from_dict(c) for n, c in d.get("synthesis", {}).items()},
            commands=d.get("commands", []),
            synthesis_baseline_sha=d.get("synthesis_baseline_sha", ""),
            mutation_testing_target=d.get("mutation_testing_target", ""),
            pass_sentinels=d.get("pass_sentinels", ["[SIM_RESULT] PASSED"]),
            fail_sentinels=d.get("fail_sentinels", ["[SIM_RESULT] FAILED"]),
        )

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict."""
        d = {
            "targets": self.targets,
            "defines": self.defines,
            "testbench_top": self.testbench_top,
            "sim_eda_tool": self.sim_eda_tool,
            "synth_eda_tool": self.synth_eda_tool,
            "lint_eda_tool": self.lint_eda_tool,
            "synthesis": {n: c.to_dict() for n, c in self.synthesis.items()},
        }
        if self.parameters:
            d["parameters"] = self.parameters
        if self.top_level_tb:
            d["top_level_tb"] = self.top_level_tb
        if self.commands:
            d["commands"] = self.commands
        if self.synthesis_baseline_sha:
            d["synthesis_baseline_sha"] = self.synthesis_baseline_sha
        if self.mutation_testing_target:
            d["mutation_testing_target"] = self.mutation_testing_target
        d["pass_sentinels"] = self.pass_sentinels
        d["fail_sentinels"] = self.fail_sentinels
        return d
