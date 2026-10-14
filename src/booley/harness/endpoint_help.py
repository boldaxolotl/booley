"""Render canonical endpoint help without admitting endpoint execution."""

from __future__ import annotations

import argparse
import copy
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_cli_parser
from booley.flows.cli_selection import _replace_action, invocation_context
from booley.flows.endpoint_cli import human_endpoint_parser
from booley.flows.endpoint_context import EndpointContext
from booley.runtime import runtime_context


class _HelpRequestedError(Exception):
    parser: argparse.ArgumentParser

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


class _SignalHelpAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        raise _HelpRequestedError(parser)


class _InertAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """Keep argument arity while leaving extension callbacks uncalled."""


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


def _prepare_parser(parser, *, inert: bool, visited: set[int]) -> None:
    if id(parser) in visited:
        return
    visited.add(id(parser))
    for action in list(parser._actions):
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _prepare_parser(child, inert=inert, visited=visited)
            continue
        if type(action) is argparse._HelpAction:
            replacement_type = _SignalHelpAction
        elif inert:
            replacement_type = _InertAction
        else:
            continue
        index = parser._actions.index(action)
        _replace_action(parser, action, replacement_type)
        replacement = parser._actions[index]
        replacement.help = action.help
        if inert:
            replacement.type = None
            replacement.choices = None


def _parser_copy(parser: argparse.ArgumentParser, *, inert: bool) -> argparse.ArgumentParser:
    copied = copy.deepcopy(parser)
    _prepare_parser(copied, inert=inert, visited=set())
    return copied


def _parse_help(parser, argv) -> tuple[_HelpRequestedError | SystemExit | None, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = None
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            parser.parse_args(argv)
        except (_HelpRequestedError, SystemExit) as exc:
            result = exc
    return result, stdout.getvalue(), stderr.getvalue()


def _emit(stdout: str, stderr: str) -> None:
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)


def try_render_help(endpoint: Any, argv: list[str], selected: Path | None) -> None:
    """Exit only for real help or a terminal parser result; otherwise return."""
    with invocation_context(selected):
        parser = (
            build_cli_parser(endpoint)
            if isinstance(endpoint, BuiltinFlow)
            else human_endpoint_parser(endpoint)
        )
        shape, _, _ = _parse_help(_parser_copy(parser, inert=True), argv)
        if shape is None:
            return
        result, stdout, stderr = _parse_help(_parser_copy(parser, inert=False), argv)
    if isinstance(result, _HelpRequestedError):
        _emit(stdout, stderr)
        result.parser.print_help()
        raise SystemExit(0)
    if isinstance(result, SystemExit) and (
        result.code or runtime_context.inside_session_runtime()
    ):
        _emit(stdout, stderr)
        raise result
