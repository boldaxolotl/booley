"""Human CLI vocabulary, separate from endpoint transport parsers."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

INVOCATION_ORIGIN_ENV = "_BOOLEY_CLI_INVOCATION_ORIGIN"
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.ASCII)


@dataclass(frozen=True)
class Duration:
    """A validated CLI duration; internal requests continue using milliseconds."""

    milliseconds: int


@dataclass(frozen=True)
class ProjectSelection:
    """Caller input with an explicit canonical-discovery or literal-alias policy."""

    value: str | Path
    legacy: bool = False


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


@dataclass(frozen=True)
class CliInvocation:
    """One CLI call's origin and outer Project selection, passed by its caller."""

    project: Path | None = None
    transport: bool = False

    @classmethod
    def of(cls, endpoint) -> CliInvocation:
        """The caller-supplied invocation, else the subprocess-boundary marker."""
        return endpoint._cli_invocation or cls(
            transport=os.environ.get(INVOCATION_ORIGIN_ENV) == "transport"
        )


def warn_alias(option: str, replacement: str) -> None:
    """Human parsers only: transport parsers never install alias actions."""
    print(
        f"booley: {option} is deprecated; use {replacement} instead "
        "(removal after one compatibility release)",
        file=sys.stderr,
    )


class SelectionAction(argparse.Action):
    """Record one selector per parser level, without subparser default loss."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        if hasattr(namespace, self.dest):
            parser.error("multiple Project selectors are not supported")
        legacy = option_string not in {"-C", "--project"}
        setattr(namespace, self.dest, ProjectSelection(values, legacy))
        if legacy:
            warn_alias(str(option_string), "--project")


class CheatSelectionAction(SelectionAction):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        if values is None:
            namespace.project = True
            if not getattr(namespace, "_cli_cheat_project_notice", False):
                warn_alias("--project", "--project-files")
                namespace._cli_cheat_project_notice = True
        else:
            super().__call__(parser, namespace, values, option_string)


class ProjectHelpFormatter(argparse.HelpFormatter):
    """Advertise the canonical path form of cheat's compatibility action."""

    def _format_action_invocation(self, action):
        if isinstance(action, CheatSelectionAction):
            return "--project PATH"
        return super()._format_action_invocation(action)

    def _format_usage(self, usage, actions, groups, prefix):
        return (
            super()
            ._format_usage(usage, actions, groups, prefix)
            .replace("--project [PATH]", "--project PATH")
        )


def add_project_option(parser: argparse.ArgumentParser, dest: str, *, cheat: bool = False) -> None:
    parser.add_argument(
        "-C",
        dest=dest,
        action=SelectionAction,
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Select the Project Root (default: discover from cwd)",
    )
    parser.add_argument(
        "--project",
        dest=dest,
        action=CheatSelectionAction if cheat else SelectionAction,
        default=argparse.SUPPRESS,
        metavar="PATH",
        **({"nargs": "?"} if cheat else {}),
        help="Select the Project Root",
    )


def collect_project_selection(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    vars(args).pop("_cli_cheat_project_notice", None)
    choices = [vars(args).pop(key) for key in list(vars(args)) if key.startswith("_cli_project_")]
    if len(choices) > 1:
        parser.error("multiple Project selectors are not supported")
    if choices:
        args._cli_selection = choices[0]


def resolve_selection(value: str | Path, *, legacy: bool = False) -> Path:
    """Canonical paths discover a checkout; aliases retain literal resolution."""
    from booley.runtime.booley_dir import reset_cache
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    reset_cache()
    path = Path(value).resolve()
    if not path.is_dir():
        raise ProjectRootDiscoveryError(f"Project path must be an existing directory: {value}")
    if legacy:
        return path
    return discover_project_root(path, explicit_selection=True)


class EndpointProjectAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Path,
        option_string: str | None = None,
    ) -> None:
        _claim_control(parser, namespace, "project")
        legacy = option_string == "--work-dir"
        if legacy:
            warn_alias("--work-dir", "--project")
        try:
            namespace.work_dir = resolve_selection(values, legacy=legacy)
        except RuntimeError as exc:
            parser.error(str(exc))


class TimeoutAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Duration | int,
        option_string: str | None = None,
    ) -> None:
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


def apply_project_selection(parser: argparse.ArgumentParser, project: Path | None) -> None:
    """Apply the outer invocation's explicit selection to a human parser."""
    if project is not None:
        parser.set_defaults(work_dir=project, _cli_seen_project=True)
