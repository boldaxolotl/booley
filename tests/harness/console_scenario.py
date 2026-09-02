"""Reusable deterministic scenario for Console app tests.

The scenario keeps the event stream and its numeric oracle together so
interaction, layout, and protocol tests do not invent incompatible runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.harness.console.app import ConsoleApp, ConsolePhase
from booley.harness.console.events import (
    AgentThinking,
    CriteriaChanged,
    McpToolCompleted,
    McpToolProgress,
    McpToolStarted,
    SetupProgress,
)
from booley.harness.console.widgets import TicketHeader


class ConsoleTestApp(ConsoleApp):
    """ConsoleApp using the production stylesheet from the source tree."""

    CSS_PATH = str(
        Path(__file__).resolve().parents[2]
        / "src"
        / "booley"
        / "harness"
        / "console"
        / "console.tcss"
    )


@dataclass
class ExpectedLedger:
    """Expected cumulative status counters for outermost completions."""

    output_tokens: int = 0
    cost_usd: float = 0.0
    lines_added: int = 0
    lines_removed: int = 0

    def add(
        self,
        *,
        output_tokens: int,
        cost_usd: float,
        lines_added: int,
        lines_removed: int,
    ) -> None:
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd
        self.lines_added += lines_added
        self.lines_removed += lines_removed


INITIAL_CRITERIA = {
    "lint_clean": {"met": True, "mandatory": True, "detail": {"warnings": 0}, "params": {}},
    "sim_pass": {
        "met": False,
        "mandatory": True,
        "detail": {"exit_code": 1},
        "params": {},
    },
    "stale_review": {
        "met": False,
        "mandatory": True,
        "stale": True,
        "detail": {},
        "params": {},
    },
    "synthesis_ok": {"met": False, "mandatory": True, "detail": {}, "params": {}},
    "coverage_sim_small": {"met": False, "mandatory": False, "detail": {}, "params": {}},
    "coverage_sim_large": {"met": False, "mandatory": False, "detail": {}, "params": {}},
    "rtl_plan_done": {"met": True, "mandatory": True, "detail": {}, "params": {}},
    "_internal": {"met": False, "mandatory": True, "detail": {}, "params": {}},
}


class ConsoleScenario:
    """Drive a ConsoleTestApp through one canonical mixed ticket run."""

    def __init__(self, app: ConsoleApp) -> None:
        self.app = app
        self.ledger = ExpectedLedger()

    def post_setup(self) -> None:
        for line in ("loading backend", "running preflight", "parsing ticket"):
            self.app.post_message(SetupProgress(line))
        self.app.query_one(TicketHeader).set_ticket_info(
            "console-scenario",
            "bugfix",
            "main",
        )
        self.app.post_message(CriteriaChanged(INITIAL_CRITERIA))

    def start_running(self) -> None:
        self.app.transition_to(ConsolePhase.RUNNING)
        self.app.post_message(AgentThinking("Inspecting rtl/top.sv\n\nPlanning the repair"))

    def complete_endpoint(
        self,
        name: str,
        target: str | None,
        *,
        exit_code: int = 0,
        duration_s: float = 2.5,
        cost_usd: float = 0.0,
        output_tokens: int = 0,
        lines_added: int = 0,
        lines_removed: int = 0,
        summary: str = "done",
        progress: tuple[str, ...] = (),
        display_lines: list[str] | None = None,
    ) -> None:
        self.app.post_message(McpToolStarted(name, target))
        for line in progress:
            self.app.post_message(McpToolProgress(line))
        self.app.post_message(
            McpToolCompleted(
                name,
                target,
                exit_code,
                duration_s,
                cost_usd,
                summary,
                output_tokens=output_tokens,
                lines_added=lines_added,
                lines_removed=lines_removed,
                display_lines=display_lines,
            )
        )
        self.ledger.add(
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            lines_added=lines_added,
            lines_removed=lines_removed,
        )

    def post_mixed_endpoints(self) -> None:
        self.complete_endpoint("lint", "rtl", summary="clean")
        self.complete_endpoint(
            "sim",
            "a-very-long-target-name-that-wraps-on-small-terminals",
            exit_code=1,
            duration_s=59.9,
            summary="assertion failed",
            progress=("compiling", "running seed 7"),
            display_lines=["tb/top_tb.sv:41: assertion failed"],
        )
        self.complete_endpoint(
            "reviewer",
            "correctness",
            cost_usd=0.004,
            output_tokens=1499,
            summary="one finding",
        )
        self.complete_endpoint(
            "tb_coder",
            "tb/top_tb.sv",
            cost_usd=0.006,
            output_tokens=1501,
            lines_added=12,
            lines_removed=3,
            summary="fixed testbench",
        )

    def add_history(self, count: int) -> None:
        for index in range(count):
            self.complete_endpoint(
                "sim",
                f"cfg_{index:02d}",
                duration_s=float(index + 1),
                summary=f"completion {index:02d}",
            )
