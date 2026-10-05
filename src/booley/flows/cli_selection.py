"""Human CLI vocabulary, separate from endpoint transport parsers."""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

INVOCATION_ORIGIN_ENV = "_BOOLEY_CLI_INVOCATION_ORIGIN"
_selection: ContextVar[Path | None] = ContextVar("cli_project_selection", default=None)
_transport: ContextVar[bool] = ContextVar("cli_transport", default=False)
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.ASCII)


@dataclass(frozen=True)
class Duration:
    """A validated CLI duration; internal requests continue using milliseconds."""

    milliseconds: int


def parse_duration(value: str) -> Duration:
    """Accept positive bare seconds or descending integer h/m/s components."""
    if len(value) > 4096:
        raise argparse.ArgumentTypeError("duration is too long")
    if re.fullmatch(r"[0-9]+", value):
        milliseconds = int(value) * 1000
    elif (match := _DURATION.fullmatch(value)) and any(match.groups()):
        milliseconds = sum(
            int(part or 0) * multiplier
            for part, multiplier in zip(match.groups(), (3600000, 60000, 1000), strict=True)
        )
    else:
        raise argparse.ArgumentTypeError(
            "use positive seconds or a duration such as 90s, 30m, 1h30m"
        )
    if milliseconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return Duration(milliseconds)


def transport_invocation() -> bool:
    """Explicit parser metadata only; this grants no execution authority."""
    return _transport.get() or os.environ.get(INVOCATION_ORIGIN_ENV) == "transport"


@contextmanager
def invocation_context(project: Path | None = None, *, transport: bool = False) -> Iterator[None]:
    """Scope same-process invocation metadata and restore it even after failure."""
    selection_token = _selection.set(project)
    transport_token = _transport.set(transport)
    try:
        yield
    finally:
        _transport.reset(transport_token)
        _selection.reset(selection_token)


def warn_alias(option: str, replacement: str) -> None:
    if not transport_invocation():
        print(
            f"booley: {option} is deprecated; use {replacement} instead "
            "(removal after one compatibility release)",
            file=sys.stderr,
        )


class SelectionAction(argparse.Action):
    """Record one selector per parser level, without subparser default loss."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if hasattr(namespace, self.dest):
            parser.error("multiple Project selectors are not supported")
        legacy = option_string not in {"-C", "--project"}
        setattr(namespace, self.dest, (values, legacy))
        # Persisted prepare is a stable internal protocol.
        if legacy and "session prepare" not in parser.prog:
            warn_alias(str(option_string), "--project")


class CheatSelectionAction(SelectionAction):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if values is None:
            namespace.project = True
            warn_alias("--project", "--project-files")
        else:
            super().__call__(parser, namespace, values, option_string)


class ProjectHelpFormatter(argparse.HelpFormatter):
    """Advertise the canonical path form of cheat's compatibility action."""

    def _format_action_invocation(self, action):
        if isinstance(action, CheatSelectionAction):
            return "--project PATH"
        return super()._format_action_invocation(action)

    def _format_actions_usage(self, actions, groups):
        return (
            super()
            ._format_actions_usage(actions, groups)
            .replace("--project [PATH]", "--project PATH")
        )


def add_project_option(parser: argparse.ArgumentParser, dest: str, *, cheat: bool = False) -> None:
    parser.add_argument(
        "-C",
        dest=dest,
        action=SelectionAction,
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Select the Project checkout (default: discover from cwd)",
    )
    parser.add_argument(
        "--project",
        dest=dest,
        action=CheatSelectionAction if cheat else SelectionAction,
        default=argparse.SUPPRESS,
        metavar="PATH",
        **({"nargs": "?"} if cheat else {}),
        help="Select the Project checkout",
    )


def collect_project_selection(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    choices = [vars(args).pop(key) for key in list(vars(args)) if key.startswith("_cli_project_")]
    if len(choices) > 1:
        parser.error("multiple Project selectors are not supported")
    if choices:
        args._cli_selection = choices[0]


def resolve_selection(value: str | Path, *, legacy: bool = False) -> Path:
    """Canonical paths discover a checkout; aliases retain literal resolution."""
    from booley.runtime.project_dir import reset_cache
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    reset_cache()
    path = Path(value).resolve()
    if not path.is_dir():
        raise ProjectRootDiscoveryError(f"Project path must be an existing directory: {value}")
    if legacy:
        return path
    return discover_project_root(path, explicit_selection=True)


class EndpointProjectAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        _claim_control(parser, namespace, "project")
        legacy = option_string == "--work-dir"
        if legacy:
            warn_alias("--work-dir", "--project")
        try:
            namespace.work_dir = resolve_selection(values, legacy=legacy)
        except RuntimeError as exc:
            parser.error(str(exc))


class TimeoutAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        _claim_control(parser, namespace, "timeout")
        if option_string == "--timeout-ms":
            warn_alias("--timeout-ms", "--timeout")
        namespace.timeout_ms = values.milliseconds if isinstance(values, Duration) else values


def _claim_control(parser, namespace, control: str) -> None:
    key = f"_cli_seen_{control}"
    if getattr(namespace, key, False):
        parser.error(f"multiple {control} options are not supported")
    setattr(namespace, key, True)


def normalize_endpoint_args(args: argparse.Namespace) -> None:
    for key in ("_cli_seen_project", "_cli_seen_timeout"):
        vars(args).pop(key, None)


def human_parser(
    transport_parser: argparse.ArgumentParser, *, durations: bool
) -> argparse.ArgumentParser:
    """Project a parser without changing schema hooks or eager extension hooks."""
    parser = copy.deepcopy(transport_parser)
    occupied = parser._option_string_actions
    old = occupied.get("--work-dir")
    if old is None or old.dest != "work_dir":
        return parser
    _replace_action(parser, old, EndpointProjectAction)
    if "--project" not in occupied and "-C" not in occupied:
        parser.add_argument(
            "-C",
            "--project",
            dest="work_dir",
            action=EndpointProjectAction,
            default=argparse.SUPPRESS,
            metavar="PATH",
            help="Select the Project checkout",
        )
    if durations and "--timeout" not in occupied and "--timeout-ms" in occupied:
        old = occupied["--timeout-ms"]
        scope = old.help.replace(" in positive milliseconds", "").replace(" in milliseconds", "")
        _replace_action(parser, old, TimeoutAction)
        parser.add_argument(
            "--timeout",
            dest="timeout_ms",
            type=parse_duration,
            action=TimeoutAction,
            default=argparse.SUPPRESS,
            metavar="DURATION",
            help=f"{scope} Use seconds, 30m, or 1h30m.",
        )
    if _selection.get() is not None:
        parser.set_defaults(work_dir=_selection.get(), _cli_seen_project=True)
    return parser


def _replace_action(parser, old, action_type) -> None:
    """Replace a copied transport control, keeping defaults and conversion."""
    replacement = action_type(
        option_strings=old.option_strings,
        dest=old.dest,
        nargs=old.nargs,
        const=old.const,
        default=old.default,
        type=old.type,
        choices=old.choices,
        required=old.required,
        help=argparse.SUPPRESS,
        metavar=old.metavar,
    )
    index = parser._actions.index(old)
    parser._actions[index] = replacement
    for group in parser._action_groups:
        if old in group._group_actions:
            group._group_actions[group._group_actions.index(old)] = replacement
    for group in parser._mutually_exclusive_groups:
        if old in group._group_actions:
            group._group_actions[group._group_actions.index(old)] = replacement
    for option in old.option_strings:
        parser._option_string_actions[option] = replacement


def extract_project_tail(
    parser: argparse.ArgumentParser, tail: list[str]
) -> tuple[list[str], list[tuple[str, bool]]]:
    """Walk known option arities; never interpret opaque values or payloads."""
    result: list[str] = []
    selections: list[tuple[str, bool]] = []
    index = 1 if tail and tail[0] == "--" else 0
    if index:
        result.append("--")
    while index < len(tail):
        token = tail[index]
        option, equals, value = token.partition("=")
        if token.startswith("-C") and len(token) > 2:
            option, equals, value = "-C", "=", token[2:]
        if option in {"--project", "-C", "--work-dir"}:
            index += 1
            if not equals:
                if index >= len(tail) or tail[index].startswith("-"):
                    parser.error(f"{option} requires PATH")
                value = tail[index]
                index += 1
            selections.append((value, option == "--work-dir"))
            if option == "--work-dir":
                warn_alias(option, "--project")
            continue
        action = parser._option_string_actions.get(option)
        if action is None or token == "--":
            result.extend(tail[index:])
            break
        result.append(token)
        index += 1
        end = _option_value_end(action, tail, index, attached=bool(equals))
        result.extend(tail[index:end])
        index = end
    return result, selections


def _option_value_end(action, tail: list[str], index: int, *, attached: bool) -> int:
    if attached or action.nargs == 0:
        return index
    if action.nargs is None or isinstance(action.nargs, int):
        count = 1 if action.nargs is None else action.nargs
        return min(index + count, len(tail))
    if action.nargs in {"?", "*", "+"}:
        end = index
        while end < len(tail) and not tail[end].startswith("-"):
            end += 1
            if action.nargs == "?":
                break
        return end
    return len(tail)
