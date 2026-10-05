"""CLI adapter for typed built-in requests; no concrete Flow selection."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from booley.flows.endpoint_cli import add_common_args, apply_environment, normalize_target_arg
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.execution_persistence import FlowExecutionAdapter


def build_parser(flow: BuiltinFlow, *, human: bool = False) -> argparse.ArgumentParser:
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
    if human:
        from booley.flows.cli_selection import human_parser

        return human_parser(parser, durations=True)
    return parser


def parse_request(flow: BuiltinFlow, argv: list[str] | None = None) -> FlowRequest:
    from booley.flows.cli_selection import normalize_endpoint_args, transport_invocation

    parser = build_parser(flow, human=not transport_invocation())
    args = parser.parse_args(argv)
    normalize_endpoint_args(args)
    normalize_target_arg(args)
    flow.argument_adapter.normalize(args, parser)
    request = flow.request_type(**vars(args))
    apply_environment(request, flow.endpoint_kind)
    return request


def execute_cli(
    flow: BuiltinFlow,
    argv: list[str] | None = None,
    *,
    adapter: FlowExecutionAdapter | None = None,
) -> ExecutionResult:
    from booley.flows.execution_persistence import StandaloneFlowExecution
    from booley.flows.flow_session import FlowSession

    request = parse_request(flow, argv)
    flow.context = FlowSession(flow, adapter or StandaloneFlowExecution())
    flow.context._args = request
    flow.context._raw_argv = argv if argv is not None else sys.argv[1:]
    flow.context._console_publication_requested = True
    return flow.context.execute_prepared()
