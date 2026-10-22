"""Human-only help projection for Flow and Specialist command-line parsers.

Flow and Specialist declarations feed two surfaces: the MCP tool schema an
agent reads and the ``--help`` a design engineer reads. The declarations here
carry the human wording so neither audience gets the other's syntax.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import partial
from typing import Any

EXAMPLE_HEADING = "Example:"


@dataclass(frozen=True)
class CliHelp:
    """What a command does and when to use it, plus one runnable example."""

    summary: str
    example: str


class SurfaceHelp(str):
    """Argument help for the MCP schema that also carries its ``--help`` wording.

    The string value is the agent-facing text, so schema extraction is
    unchanged; ``cli`` replaces it when a human parser is presented.
    """

    cli: str

    def __new__(cls, mcp: str, *, cli: str) -> SurfaceHelp:
        text = super().__new__(cls, mcp)
        text.cli = cli
        return text

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, str]]:
        """Keep both wordings when argparse parsers are copied."""
        return (str(self),), {"cli": self.cli}


class CommandHelpFormatter(argparse.HelpFormatter):
    """Wrap prose but keep the example command on its own line."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        blocks = []
        for block in text.split("\n\n"):
            if block.startswith(EXAMPLE_HEADING):
                blocks.append("\n".join(indent + line for line in block.splitlines()))
            else:
                blocks.append(super()._fill_text(block, width, indent))
        return "\n\n".join(blocks)


def present_command(parser: argparse.ArgumentParser, endpoint: Any) -> argparse.ArgumentParser:
    """Give a human parser its full command name and design-engineer wording."""
    kind = getattr(endpoint, "endpoint_kind", None)
    name = getattr(endpoint, "name", "")
    if kind in {"flow", "specialist"} and name:
        parser.prog = f"booley {kind} {name}"
    cli_help = getattr(endpoint, "cli_help", None)
    if cli_help is not None:
        parser.description = cli_help.summary
        parser.epilog = f"{EXAMPLE_HEADING}\n  {cli_help.example}"
        # An extension that chose its own formatter keeps it.
        if parser.formatter_class is argparse.HelpFormatter:
            parser.formatter_class = CommandHelpFormatter
    for action in parser._actions:
        if isinstance(action.help, SurfaceHelp):
            action.help = action.help.cli
    return parser


@dataclass(frozen=True)
class HelpOption:
    """Presentation for a destination, including an explicitly declared pair."""

    dest: str
    omission: str
    description: str = ""
    pair: tuple[str, str] | None = None


@dataclass(frozen=True)
class HelpGroup:
    """Ordered human controls; parsing and transport declarations stay separate."""

    title: str
    options: tuple[HelpOption, ...]
    description: str | None = None


EXPERT_GUIDANCE = (
    "Defaults are tuned; change these controls only when investigating timing or area."
)
BASELINE_HELP = HelpOption(
    "baseline",
    "When omitted, use the baseline pinned at Goal entry for relative criteria; "
    "otherwise run without baseline comparison.",
)
PPA_HELP = HelpOption(
    "ppa_profile",
    "When omitted, use Target flow_options and the default profile. "
    "An explicit profile skips configured backend overrides; explicit expert overrides still apply.",
)


class FlowHelpFormatter(CommandHelpFormatter):
    """Combine only declared boolean pairs in the help body, preserving usage."""

    def __init__(self, prog: str, *, pairs: tuple[tuple[str, str], ...], **kwargs: int) -> None:
        super().__init__(prog, **kwargs)
        self.pairs: dict[str, str] = dict(pairs)
        self.negative_options: set[str] = {negative for _, negative in pairs}

    def add_arguments(self, actions: list[argparse.Action]) -> None:
        super().add_arguments(
            [a for a in actions if not self.negative_options.intersection(a.option_strings)]
        )

    def _format_action_invocation(self, action: argparse.Action) -> str:
        for option in action.option_strings:
            if option in self.pairs:
                return f"{option}, {self.pairs[option]}"
        return super()._format_action_invocation(action)


def shared_help(flow_name: str, *, target_required: bool) -> tuple[HelpGroup, ...]:
    """Describe resolver-owned defaults without exposing transport aliases."""
    target = (
        "Required: omission is a parse error."
        if target_required
        else "When omitted, exactly one eligible Target is selected and announced; "
        "zero or multiple eligible Targets fail and list the eligible Targets."
    )
    if flow_name == "sim":
        target += (
            " On resume the manifest owns selection and an explicit Target "
            "conflicts with --resume-from."
        )
    return (
        HelpGroup(
            "Common",
            (
                HelpOption(
                    "work_dir", "When omitted, discover the Project from the current directory."
                ),
                HelpOption("target", target),
                HelpOption("diagnostic", "Default: off."),
                HelpOption("dry_run", "Default: off."),
                HelpOption(
                    "timeout_ms",
                    f"When omitted, use [flows.{flow_name}].timeout_ms, "
                    "or the Flow's default active-time budget when not configured.",
                ),
            ),
        ),
        HelpGroup(
            "Output",
            (
                HelpOption(
                    "report_dir",
                    "When omitted, use the runtime report location, otherwise flow-reports "
                    "under the resolved Booley Directory.",
                ),
            ),
        ),
    )


def project_help(
    parser: argparse.ArgumentParser, groups: tuple[HelpGroup, ...]
) -> argparse.ArgumentParser:
    """Rehome copied actions without changing parsing or exclusive membership.

    This is the sole bookkeeping seam for private argparse group lists. Hidden
    compatibility aliases retain SUPPRESS and still belong to their named group.
    """
    presentations = {option.dest: (group, option) for group in groups for option in group.options}
    pairs = tuple(option.pair for _, option in presentations.values() if option.pair is not None)
    parser.formatter_class = partial(FlowHelpFormatter, pairs=pairs)
    named = {}
    for group in groups:
        if group.title not in named:
            named[group.title] = parser.add_argument_group(group.title, group.description)
    for action in parser._actions:
        if action.dest == "help":
            continue
        group, option = presentations[action.dest]
        for original in parser._action_groups:
            if action in original._group_actions:
                original._group_actions.remove(action)
        named[group.title]._group_actions.append(action)
        if action.help != argparse.SUPPRESS:
            description = option.description or action.help
            assert description, f"Missing human description for {action.dest}"
            action.help = f"{description.rstrip('.')}. {option.omission}"
    return parser


def add_quiet(parser: argparse.ArgumentParser, *, human: bool) -> None:
    """Add CLI-only quiet directly to Output on the human parser."""
    owner = parser
    if human:
        owner = next(group for group in parser._action_groups if group.title == "Output")
    owner.add_argument(
        "-q",
        "--quiet",
        dest="_console_quiet",
        action="store_true",
        help="Suppress human progress, live EDA output and log footers. Default: off.",
    )
