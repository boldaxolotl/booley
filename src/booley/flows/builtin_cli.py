"""CLI adapter for typed built-in requests; no concrete Flow selection."""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

from booley.flows.endpoint_cli import add_common_args, apply_environment, normalize_target_arg
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.execution_persistence import FlowExecutionAdapter


def build_parser(flow: BuiltinFlow) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=flow.name,
        description=flow.description,
        allow_abbrev=False,
    )
    add_common_args(
        parser,
        target_required=flow.target_required,
        accepts_target=flow.accepts_target,
        target_help=flow.target_help,
    )
    flow.argument_adapter.add_common_args(parser)
    flow.argument_adapter.add_args(parser)
    return parser


def build_cli_parser(flow: BuiltinFlow) -> argparse.ArgumentParser:
    """Layer human presentation onto a fresh parser, outside the MCP schema."""
    parser = build_parser(flow)
    parser.add_argument(
        "-q",
        "--quiet",
        dest="_console_quiet",
        action="store_true",
        help="Suppress human progress, live EDA output and log footers",
    )
    return parser


def _parse_cli(flow: BuiltinFlow, argv: list[str] | None):
    parser = build_cli_parser(flow)
    args = parser.parse_args(argv)
    quiet = vars(args).pop("_console_quiet")
    normalize_target_arg(args)
    flow.argument_adapter.normalize(args, parser)
    request = flow.request_type(**vars(args))
    apply_environment(request, flow.endpoint_kind)
    return request, quiet


def parse_request(flow: BuiltinFlow, argv: list[str] | None = None) -> FlowRequest:
    return _parse_cli(flow, argv)[0]


def execute_cli(
    flow: BuiltinFlow,
    argv: list[str] | None = None,
    *,
    adapter: FlowExecutionAdapter | None = None,
) -> ExecutionResult:
    from booley.flows.execution_persistence import StandaloneFlowExecution
    from booley.flows.flow_session import FlowSession

    request, quiet = _parse_cli(flow, argv)
    flow.context = FlowSession(flow, adapter or StandaloneFlowExecution())
    flow.context._args = request
    flow.context._raw_argv = argv if argv is not None else sys.argv[1:]
    flow.context._console_publication_requested = True
    if (
        quiet
        or os.environ.get("BOOLEY_RUNTIME_DIR")
        or not isinstance(flow.context.execution_adapter, StandaloneFlowExecution)
        or getattr(request, "dry_run", False)
    ):
        return flow.context.execute_prepared()
    from booley.flows.terminal_progress import TerminalProgress

    observer = TerminalProgress(
        flow.name,
        request.target or "(selection)",
        request.work_dir,
        stream=sys.stderr,
    )
    flow.context.terminal_progress = observer
    try:
        with observer.installed():
            return flow.context.execute_prepared()
    finally:
        flow.context.terminal_progress = None
