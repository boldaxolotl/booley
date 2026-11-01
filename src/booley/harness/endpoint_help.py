"""Render canonical endpoint help without admitting endpoint execution."""

from __future__ import annotations

import argparse
import copy
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NoReturn

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_cli_parser
from booley.flows.cli_selection import CliInvocation, EndpointProjectAction, ProjectSelection
from booley.flows.endpoint_cli import human_endpoint_parser, parser_settings
from booley.flows.endpoint_context import EndpointContext
from booley.runtime import runtime_context


class _HelpRequestedError(Exception):
    parser: argparse.ArgumentParser

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


class _ParserSyntaxError(Exception):
    parser: argparse.ArgumentParser
    message: str

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        self.parser = parser
        self.message = message


class _HelpProbeParser(argparse.ArgumentParser):
    """Signal standard help; an inert probe keeps arity but skips conversion and callbacks.

    A selection probe also records the values argparse assigns to Project selectors.
    """

    inert = False
    selections: list[ProjectSelection] | None = None

    def error(self, message: str) -> NoReturn:
        raise _ParserSyntaxError(self, message)

    def _get_value(self, action: argparse.Action, arg_string: str) -> Any:
        # argparse also converts unseen string defaults here; an inert probe must not.
        return arg_string if self.inert else super()._get_value(action, arg_string)

    def _get_values(self, action: argparse.Action, arg_strings: list[str]) -> Any:
        if type(action) is argparse._HelpAction:
            if self.selections is not None:
                return argparse.SUPPRESS
            raise _HelpRequestedError(self)
        if self.selections is not None and isinstance(action, EndpointProjectAction):
            self.selections.append(
                ProjectSelection(arg_strings[0], legacy="--work-dir" in action.option_strings)
            )
        if self.inert and not isinstance(action, argparse._SubParsersAction):
            return argparse.SUPPRESS
        return super()._get_values(action, arg_strings)


def supports_shared_cli(endpoint: Any) -> bool:
    """Match the shared entrypoint contract used by outer Project selection."""
    if isinstance(endpoint, BuiltinFlow):
        return True
    return (
        isinstance(endpoint, EndpointContext)
        and type(endpoint).main is EndpointContext.main
        and type(endpoint).parse_args is EndpointContext.parse_args
        and type(endpoint).execute_cli is EndpointContext.execute_cli
        and type(endpoint)._prepare_cli_execution is EndpointContext._prepare_cli_execution
        and "--work-dir" in endpoint._parser._option_string_actions
        and endpoint._parser._option_string_actions["--work-dir"].dest == "work_dir"
    )


def _copy_parser_state(source, target) -> None:
    """Retain copied container identity, including groups used by dynamic actions."""
    target._registries = source._registries
    target._actions = source._actions
    target._option_string_actions = source._option_string_actions
    target._defaults = source._defaults
    target._action_groups = source._action_groups
    target._mutually_exclusive_groups = source._mutually_exclusive_groups
    target._positionals = source._positionals
    target._optionals = source._optionals
    target._subparsers = source._subparsers
    target._negative_number_matcher = source._negative_number_matcher
    target._has_negative_number_optionals = source._has_negative_number_optionals


def _probe_parser(
    parser, visited: dict[int, argparse.ArgumentParser], *, inert: bool
) -> argparse.ArgumentParser:
    if id(parser) in visited:
        return visited[id(parser)]
    wrapped = _HelpProbeParser(add_help=False, **parser_settings(parser))
    wrapped.inert = inert
    _copy_parser_state(parser, wrapped)
    visited[id(parser)] = wrapped
    for action in wrapped._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Dispatch and presentation share this mapping; retain its identity.
            action.choices.update(
                {
                    name: _probe_parser(child, visited, inert=inert)
                    for name, child in action.choices.items()
                }
            )
    return wrapped


def _parser_copy(parser: argparse.ArgumentParser, *, inert: bool) -> argparse.ArgumentParser:
    return _probe_parser(copy.deepcopy(parser), {}, inert=inert)


def _parse_help(
    parser, argv
) -> tuple[_HelpRequestedError | _ParserSyntaxError | SystemExit | None, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = None
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            parser.parse_args(argv)
        except (_HelpRequestedError, _ParserSyntaxError, SystemExit) as exc:
            result = exc
    return result, stdout.getvalue(), stderr.getvalue()


def tail_project_selections(
    parser: argparse.ArgumentParser, tail: list[str]
) -> list[ProjectSelection]:
    """The Project selectors argparse itself assigns in an endpoint tail, in order.

    Runs the endpoint's own parse over the tail without conversions or actions, so
    payload text, option values, and separator-led arguments never count. The tail
    is not rewritten; a syntax error keeps the selectors recorded before it.
    """
    probe = _probe_parser(parser, {}, inert=True)
    probe.selections = found = []
    _parse_help(probe, tail[1:] if tail[:1] == ["--"] else tail)
    return found


def _emit(stdout: str, stderr: str) -> None:
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)


def try_render_help(endpoint: Any, argv: list[str], selected: Path | None) -> None:
    """Exit only for real help or a terminal parser result; otherwise return."""
    parser = (
        build_cli_parser(endpoint, CliInvocation(project=selected))
        if isinstance(endpoint, BuiltinFlow)
        else human_endpoint_parser(endpoint, project=selected)
    )
    shape, _, _ = _parse_help(_parser_copy(parser, inert=True), argv)
    if shape is None:
        return
    result, stdout, stderr = _parse_help(_parser_copy(parser, inert=False), argv)
    if isinstance(result, _HelpRequestedError):
        _emit(stdout, stderr)
        result.parser.print_help()
        raise SystemExit(0)
    if isinstance(result, _ParserSyntaxError):
        _emit(stdout, stderr)
        result.parser.print_usage(file=sys.stderr)
        print(f"{result.parser.prog}: error: {result.message}", file=sys.stderr)
        raise SystemExit(2)
    if isinstance(result, SystemExit) and runtime_context.inside_session_runtime():
        _emit(stdout, stderr)
        raise result
