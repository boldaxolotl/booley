"""CLI adapter for typed built-in requests; no concrete Flow selection."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from booley.flows.endpoint_cli import add_common_args, apply_environment
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.execution_persistence import FlowExecutionAdapter


def build_parser(flow: BuiltinFlow) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=flow.name, description=flow.description)
    add_common_args(
        parser,
        target_required=flow.target_required,
        accepts_target=flow.accepts_target,
        target_help=flow.target_help,
    )
    flow.argument_adapter.add_common_args(parser)
    flow.argument_adapter.add_args(parser)
    return parser


def parse_request(flow: BuiltinFlow, argv: list[str] | None = None) -> FlowRequest:
    parser = build_parser(flow)
    args = parser.parse_args(argv)
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
    return flow.context.execute_prepared()
