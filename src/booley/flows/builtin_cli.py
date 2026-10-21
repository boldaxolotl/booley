"""CLI adapter for typed built-in requests; no concrete Flow selection."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.cli_help import SurfaceHelp
from booley.flows.cli_selection import CliInvocation
from booley.flows.endpoint_cli import add_common_args, apply_environment, normalize_target_arg
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.execution_persistence import FlowExecutionAdapter


def build_parser(
    flow: BuiltinFlow, *, human: bool = False, project: Path | None = None
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=flow.name,
        description=flow.description,
        allow_abbrev=False,
    )
    target_help = flow.target_help
    if flow.name == "sim":
        resume_help = " For sim resume, the manifest owns selection."
        target_help = (
            SurfaceHelp(str(target_help) + resume_help, cli=target_help.cli + resume_help)
            if isinstance(target_help, SurfaceHelp)
            else target_help + resume_help
        )
    add_common_args(
        parser,
        target_required=flow.target_required,
        accepts_target=flow.accepts_target,
        target_help=target_help,
        human=human,
    )
    flow.argument_adapter.add_common_args(parser, human=human)
    flow.argument_adapter.add_args(parser)
    if human:
        from booley.flows.cli_arguments import add_human_options
        from booley.flows.cli_help import present_command, project_help, shared_help
        from booley.flows.cli_selection import apply_project_selection

        add_human_options(parser)
        apply_project_selection(parser, project)
        groups = shared_help(flow.name, target_required=flow.target_required)
        presented = present_command(parser, flow)
        return project_help(presented, groups + flow.argument_adapter.help_groups)
    return parser


def build_cli_parser(
    flow: BuiltinFlow, invocation: CliInvocation | None = None
) -> argparse.ArgumentParser:
    """Layer human presentation onto a fresh parser, outside the MCP schema."""
    from booley.flows.cli_help import add_quiet

    invocation = invocation or CliInvocation.of(flow)
    human = not invocation.transport
    parser = build_parser(flow, human=human, project=invocation.project)
    add_quiet(parser, human=human)
    return parser


def _parse_cli(flow: BuiltinFlow, argv: list[str] | None, invocation: CliInvocation):
    from booley.flows.cli_selection import normalize_endpoint_args

    parser = build_cli_parser(flow, invocation)
    args = parser.parse_args(argv)
    quiet = vars(args).pop("_console_quiet")
    normalize_endpoint_args(args)
    normalize_target_arg(args)
    flow.argument_adapter.normalize(args, parser)
    request = flow.request_type(**vars(args))
    apply_environment(request, flow.endpoint_kind)
    return request, quiet


def parse_request(flow: BuiltinFlow, argv: list[str] | None = None) -> FlowRequest:
    return _parse_cli(flow, argv, CliInvocation.of(flow))[0]


def execute_cli(
    flow: BuiltinFlow,
    argv: list[str] | None = None,
    *,
    adapter: FlowExecutionAdapter | None = None,
) -> ExecutionResult:
    from booley.flows.execution_persistence import StandaloneFlowExecution
    from booley.flows.flow_session import FlowSession

    invocation = CliInvocation.of(flow)
    request, quiet = _parse_cli(flow, argv, invocation)
    flow.context = FlowSession(flow, adapter or StandaloneFlowExecution())
    if not invocation.transport and not flow.context._acceptance_recorder.acceptance_identity():
        from booley.harness.cli_goal_notice import warn_cli_goal_recording

        warn_cli_goal_recording(request.work_dir)
    flow.context._args = request
    flow.context._raw_argv = argv if argv is not None else sys.argv[1:]
    flow.context._console_publication_requested = True
    if (
        quiet
        or invocation.transport
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
