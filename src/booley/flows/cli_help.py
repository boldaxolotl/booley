"""Human-only help projection for the built-in Flow argument adapters."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import partial

from booley.flows.invocation import default_timeout_ms


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
    "When omitted, use the pinned Ticket baseline for relative criteria; "
    "otherwise run without baseline comparison.",
)
PPA_HELP = HelpOption(
    "ppa_profile",
    "When omitted, use Target flow_options and the default profile. "
    "An explicit profile skips configured backend overrides; explicit expert overrides still apply.",
)


class FlowHelpFormatter(argparse.HelpFormatter):
    """Combine only declared boolean pairs in the help body, preserving usage."""

    def __init__(self, prog: str, *, pairs: tuple[tuple[str, str], ...], **kwargs) -> None:
        super().__init__(prog, **kwargs)
        self.pairs = dict(pairs)
        self.negative_options = {negative for _, negative in pairs}

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
        else "When omitted, discover/select Simulation Targets; on resume the manifest owns "
        "selection and an explicit Target conflicts with --resume-from."
    )
    return (
        HelpGroup(
            "Common",
            (
                HelpOption("work_dir", "When omitted, discover the Project from cwd."),
                HelpOption("target", target),
                HelpOption("diagnostic", "Default: off."),
                HelpOption("dry_run", "Default: off."),
                HelpOption(
                    "timeout_ms",
                    f"When omitted, use [flows.{flow_name}].timeout_ms, "
                    f"or {default_timeout_ms(flow_name) // 1000}s when not configured.",
                ),
            ),
        ),
        HelpGroup(
            "Output",
            (
                HelpOption(
                    "report_dir",
                    "When omitted, use the runtime report location, otherwise flow-reports "
                    "under the resolved Project data directory.",
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
