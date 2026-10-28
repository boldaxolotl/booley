#!/usr/bin/env python3
"""booley -- CLI entry point for the Booley RTL development harness.

Subcommands:
    goal      Work with Goal Records and evidence
    chat      Open the Project's configured agent CLI
    worktree  Create a linked worktree with paired or snapshot Project inputs
    cheat     Print quick-reference cheatsheet
    doctor    Run environment health checks
    bootstrap Prepare Project-independent host resources
    init      Initialize a Project
    auth      Mint + store the agent's long-lived auth token
    flow      Run a deterministic Booley Flow directly
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import sys
import warnings
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from booley.feedback import cli as feedback_cli
from booley.feedback.storage import feedback_storage_dir
from booley.harness import cheatsheet, upgrade_cli, upgrade_review
from booley.harness.auth_cmd import run_auth
from booley.harness.bootstrap_cli import run_bootstrap
from booley.harness.chat_cmd import run as run_chat
from booley.harness.doctor import run_doctor
from booley.harness.feedback_environment import resolve_feedback_environment
from booley.harness.init_cmd import run_init
from booley.harness.render_md import render
from booley.harness.setup import cleanup_cli
from booley.harness.setup.common import configure_progress_output
from booley.harness.worktree_cmd import add_subparser as add_worktree_subparser
from booley.harness.worktree_cmd import run as run_worktree
from booley.projects import cli as project_inventory_cli
from booley.runtime import runtime_context
from booley.runtime.booley_dir import BOOLEY_DIR_NAME
from booley.runtime.docs_links import docs_url
from booley.runtime.lifecycle_lock import LifecycleLockError
from booley.runtime.paths import cheatsheet_path

if TYPE_CHECKING:
    # Type-only: keep the MCP tool registry (and endpoint packages it leads to) out
    # of the import path of every `booley` invocation.
    from booley.mcp.registry import McpToolInfo

# --- Constants ---


class CommandLocation(Enum):
    """Where one top-level command is valid, including skill commands."""

    HOST = "host"
    SESSION_RUNTIME = "Sandbox"
    EITHER = "either"

    @property
    def label(self) -> str:
        return f"[{self.value}]"


COMMAND_LOCATIONS = {
    "help": CommandLocation.EITHER,
    "goal": CommandLocation.SESSION_RUNTIME,
    "dashboard": CommandLocation.SESSION_RUNTIME,
    "chat": CommandLocation.SESSION_RUNTIME,
    "worktree": CommandLocation.SESSION_RUNTIME,
    "cheat": CommandLocation.EITHER,
    "doctor": CommandLocation.EITHER,
    "bootstrap": CommandLocation.HOST,
    "init": CommandLocation.HOST,
    "eda": CommandLocation.HOST,
    "auth": CommandLocation.HOST,
    "session": CommandLocation.HOST,
    "projects": CommandLocation.HOST,
    "upgrade": CommandLocation.EITHER,
    "targets": CommandLocation.EITHER,
    "flow": CommandLocation.SESSION_RUNTIME,
    "specialist": CommandLocation.SESSION_RUNTIME,
    "feedback": CommandLocation.EITHER,
    "cleanup": CommandLocation.EITHER,
}

_HIDDEN_COMMANDS = frozenset({"feedback", "cleanup", "upgrade", "session", "goal", "shell"})
_COMMAND_LOCATION_ORDER = {
    CommandLocation.SESSION_RUNTIME: 0,
    CommandLocation.EITHER: 1,
    CommandLocation.HOST: 2,
}


class ProjectBinding(Enum):
    """How a command selects Project context before discovery."""

    REQUIRED = "required"
    INDEPENDENT = "independent"
    OPTIONAL = "optional"
    FEEDBACK = "source-feedback"


COMMAND_PROJECT_BINDINGS = {
    "help": ProjectBinding.INDEPENDENT,
    "goal": ProjectBinding.REQUIRED,
    "dashboard": ProjectBinding.REQUIRED,
    "chat": ProjectBinding.REQUIRED,
    "worktree": ProjectBinding.REQUIRED,
    "cheat": ProjectBinding.OPTIONAL,
    "doctor": ProjectBinding.REQUIRED,
    "bootstrap": ProjectBinding.INDEPENDENT,
    "init": ProjectBinding.REQUIRED,
    "eda": ProjectBinding.INDEPENDENT,
    "auth": ProjectBinding.OPTIONAL,
    "session": ProjectBinding.REQUIRED,
    "projects": ProjectBinding.INDEPENDENT,
    "upgrade": ProjectBinding.REQUIRED,
    "targets": ProjectBinding.REQUIRED,
    "flow": ProjectBinding.REQUIRED,
    "specialist": ProjectBinding.REQUIRED,
    "feedback": ProjectBinding.FEEDBACK,
    "cleanup": ProjectBinding.REQUIRED,
    "shell": ProjectBinding.REQUIRED,
}

RETIRED_COMMAND_POINTERS = {
    command: f"`booley {command}` is not available; Goal Mode replaces it: "
    "open an agent session in the Sandbox and run /booley-goal"
    for command in ("run", "board")
}


def command_locations() -> dict[str, CommandLocation]:
    """Return human-facing commands in location order, then alphabetically."""
    return dict(
        sorted(
            (
                (command, location)
                for command, location in COMMAND_LOCATIONS.items()
                if command not in _HIDDEN_COMMANDS
            ),
            key=lambda item: (_COMMAND_LOCATION_ORDER[item[1]], item[0]),
        )
    )


def command_project_bindings() -> dict[str, ProjectBinding]:
    """Return the public command Project-binding catalog."""
    return COMMAND_PROJECT_BINDINGS


def _command_project_binding(command: str, args: argparse.Namespace) -> ProjectBinding:
    return command_project_bindings()[command]


class _RetiredEdaToolOptionAction(argparse.Action):
    """Reject retired scaffold flags with their canonical EDA-tool spelling."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        replacement = {
            "--sim-tool": "--sim-eda-tool",
            "--lint-tool": "--lint-eda-tool",
        }[str(option_string)]
        parser.error(f"{option_string} is retired; use {replacement}")


class _RootHelpAction(argparse.Action):
    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        root_parser: argparse.ArgumentParser,
        **kwargs: Any,
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)
        self._root_parser = root_parser

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        self._root_parser.print_help()
        parser.exit()


def _agent_auth_arg(value: str) -> str:
    """Normalize the CLI's hyphenated auth spelling to the TOML vocabulary."""
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"auto", "subscription", "api_key"}:
        raise argparse.ArgumentTypeError("choose auto, subscription, or api-key")
    return normalized


logger = logging.getLogger("booley")


# ---------------------------------------------------------------------------
# Project Root detection
# ---------------------------------------------------------------------------


def find_project_root() -> Path:
    """Find the main Project Root by walking up from cwd looking for .git.

    Falls back to $RTL_PROJECT_ROOT env var if set.
    """
    from booley.runtime.project_discovery import discover_project_root

    return discover_project_root()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Heartbeat-aware execution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main loop — helpers
# ---------------------------------------------------------------------------


def _force_utf8() -> None:
    """Force UTF-8 for all text I/O — prevents CP1252 encoding errors on Windows."""
    os.environ.setdefault("PYTHONUTF8", "1")
    if sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _baked_identity() -> tuple[str, str]:
    """``(commit, commit time)`` stamped into the package at wheel-build time.

    A wheel install has no adjacent ``.git``, so without the stamp every image
    would report a bare ``booley 0.1.0`` — leaving no way to confirm which
    commit was actually baked, which is exactly what the freshness check asks
    you to verify (F-5). ``booley.runtime.build_stamp.write_build_stamp``
    writes this module just before building the wheel; it is generated, not
    tracked. Unknown parts are ``""``: an older stamp has no ``COMMIT_TIME``.
    """
    try:
        from booley import _build_commit  # type: ignore[attr-defined]
    except ImportError:
        return "", ""
    return (
        getattr(_build_commit, "COMMIT", ""),
        getattr(_build_commit, "COMMIT_TIME", ""),
    )


def _code_identity() -> tuple[str, str]:
    """``(short commit, commit time)`` attributed to the imported Booley code.

    The commit may carry ``+dirty``. Live source and installed distributions
    are disjoint: a source failure never borrows a wheel stamp, and a wheel
    never borrows an enclosing repo. Unknown parts are ``""``.
    """
    import booley

    attribution = booley.version_attribution
    if attribution.distribution_name is not None:
        return _baked_identity()
    return attribution.source_git_metadata()


def _utc_date(timestamp: str) -> str:
    """UTC ``YYYY-MM-DD`` of an ISO-8601 *timestamp*, or ``""`` when unparseable."""
    from booley.runtime.timefmt import parse_timestamp

    try:
        return parse_timestamp(timestamp).astimezone(UTC).date().isoformat()
    except ValueError:
        return ""


def _version_string() -> str:
    """`booley <version>` plus commit and UTC commit date when they are known."""
    from booley import __version__

    commit, commit_time = _code_identity()
    if not commit:
        return f"booley {__version__}"
    date = _utc_date(commit_time)
    return (
        f"booley {__version__} ({commit}, {date})" if date else f"booley {__version__} ({commit})"
    )


class _PrintVersion(argparse.Action):
    """Print the version on one unwrapped line and exit.

    argparse's built-in ``version`` action re-wraps its text to the terminal
    width; this one also defers the (possibly git-backed) lookup until asked.
    """

    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        sys.stdout.write(_version_string() + "\n")
        parser.exit()


def _install_project_options(parser: argparse.ArgumentParser) -> None:
    """Install shared controls using the command ownership catalog."""
    from booley.flows.cli_selection import add_project_option

    def install(node, route):
        dest = "_cli_project_" + "_".join(route or ("root",))
        add_project_option(node, dest, cheat=route == ("cheat",))
        if route and route[0] == "feedback":
            for option in ("-C", "--project"):
                default = " (default: discover from cwd)" if option == "-C" else ""
                node._option_string_actions[option].help = (
                    f"Select the checkout for feedback context{default}; in the Sandbox, "
                    "this does not move the shared Project Findings Log"
                )
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    if not route and (
                        command_project_bindings()[name] is ProjectBinding.INDEPENDENT
                    ):
                        continue
                    install(child, (*route, name))

    install(parser, ())


def _resolve_cli_selection(args: argparse.Namespace) -> Path:
    from booley.flows.cli_selection import resolve_selection

    selection = args._cli_selection
    root = resolve_selection(selection.value, legacy=selection.legacy)
    return root


def _extract_endpoint_selection(parser, args) -> None:
    """Find a built-in endpoint's tail Project selector, leaving the tail untouched.

    The endpoint's own argparse parse says which tokens reach a selector, so option
    values and payload text can never be mistaken for one. The endpoint's real parse
    resolves the same token again, against the same checkout, when it runs.
    """
    if args.command not in {"flow", "specialist"} or not args.endpoint_name:
        return
    tail = args.endpoint_args
    # A superset of the tokens that can reach a selector, including short clusters
    # such as -qC../b; a false positive only costs one probe parse.
    if not any(
        token.split("=", 1)[0] in {"--project", "--work-dir"}
        or (token.startswith("-") and not token.startswith("--") and "C" in token)
        for token in tail
    ):
        return
    from booley.mcp.registry import discover_mcp_tools
    from booley.targets.flow_names import canonical

    name = canonical(args.endpoint_name) if args.command == "flow" else args.endpoint_name
    info = next(
        (item for item in discover_mcp_tools() if item.name == name and item.kind == args.command),
        None,
    )
    if info is None:
        return
    from booley.flows.base import BuiltinFlow
    from booley.flows.builtin_cli import build_cli_parser
    from booley.flows.cli_selection import CliInvocation
    from booley.flows.endpoint_cli import human_endpoint_parser
    from booley.harness.endpoint_help import tail_project_selections

    endpoint_cls = _load_mcp_tool_class(info)
    if endpoint_cls is None:
        return
    endpoint = endpoint_cls()
    # The parser execution will use, so discovery and execution agree on every spelling.
    endpoint_parser = (
        build_cli_parser(endpoint, CliInvocation())
        if isinstance(endpoint, BuiltinFlow)
        else human_endpoint_parser(endpoint)
    )
    selections = tail_project_selections(endpoint_parser, list(tail))
    if len(selections) + int(hasattr(args, "_cli_selection")) > 1:
        parser.error("multiple Project selectors are not supported")
    if selections:
        args._cli_selection = selections[0]
        args._cli_tail_selection = True


def _outer_endpoint_selection(args: argparse.Namespace) -> bool:
    """A selector before the endpoint name; a tail selector stays in the endpoint's argv."""
    return hasattr(args, "_cli_selection") and not getattr(args, "_cli_tail_selection", False)


def _invoke_endpoint(endpoint, argv, args, **kwargs) -> int:
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.endpoint_help import supports_shared_cli

    selected = getattr(args, "_cli_endpoint_project", None)
    supported = supports_shared_cli(endpoint)
    # A tail selector stays in argv, so an override would receive it raw.
    if (selected is not None or getattr(args, "_cli_tail_selection", False)) and not supported:
        print(
            "ERROR: endpoint overrides the shared CLI entrypoint; implement the common parser "
            "to support --project selection.",
            file=sys.stderr,
        )
        return 2
    endpoint._cli_invocation = CliInvocation(project=selected)
    return endpoint.main(argv, **kwargs)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with subcommands + legacy flags."""
    parser = argparse.ArgumentParser(
        prog="booley",
        description="Booley — RTL development harness.",
        epilog=(
            "Run bare `booley` on the host for help, or inside the Sandbox to open "
            "this Project's configured agent CLI. "
            "Locations: [Sandbox] inside the Sandbox only; [either] host or Sandbox; "
            "[host] host terminal only. "
            "See `booley cheat --skills` for guided agent workflows."
        ),
    )
    parser.add_argument(
        "--version",
        action=_PrintVersion,
        help="Show the Booley version, plus its commit and UTC commit date when known, and exit",
    )
    # metavar hides unadvertised subcommands (e.g. the debugging-only `shell`,
    # ADR 0028 Decision 2: kept working, stripped from help/docs) from the
    # usage line; subparsers added without `help=` stay out of the listing.
    sub = parser.add_subparsers(
        dest="command",
        metavar=f"{{{','.join(command_locations())}}}",
    )

    help_parser = sub.add_parser(
        "help", add_help=False, help="Show top-level help and exit (same as --help)"
    )
    help_parser.add_argument(
        "-h",
        "--help",
        action=_RootHelpAction,
        root_parser=parser,
        help="Show top-level help and exit",
    )
    add_worktree_subparser(sub)
    _add_utility_subparsers(sub)
    _add_goal_subparser(sub)
    dashboard = sub.add_parser("dashboard", help="Open the read-only Sandbox Dashboard")
    attach = dashboard.add_mutually_exclusive_group()
    attach.add_argument("--attach-policy", action="store_true", help=argparse.SUPPRESS)
    attach.add_argument("--on-attach", action="store_true", help=argparse.SUPPRESS)

    _install_project_options(parser)
    _decorate_command_help(sub)

    return parser


def _decorate_command_help(subparsers: argparse._SubParsersAction) -> None:
    """Hide skill commands and order public summaries by execution location."""
    locations = command_locations()
    advertised = {action.dest: action for action in subparsers._choices_actions}
    subparsers._choices_actions[:] = [advertised[name] for name in locations]
    for action in subparsers._choices_actions:
        action.help = f"{locations[action.dest].label} {action.help}"


def _project_root_parent(dest: str) -> argparse.ArgumentParser:
    """A reusable ``--project-root``/``-p`` alias owned by one subcommand route."""
    from booley.flows.cli_arguments import add_project_root_alias

    parent = argparse.ArgumentParser(add_help=False)
    add_project_root_alias(parent, dest, short=True)
    return parent


def _add_cheat_subparser(sub) -> None:
    """Add the `cheat` subparser: one `--<section>` flag per cheatsheet section."""
    from booley.flows.cli_selection import ProjectHelpFormatter

    cheat_p = sub.add_parser(
        "cheat", help="Print quick-reference cheatsheet", formatter_class=ProjectHelpFormatter
    )
    cheat_sections = cheat_p.add_argument_group(
        "sections", "Print only these sections (combinable; default: list section flags)"
    )
    for slug in cheatsheet.section_slugs():
        cheat_sections.add_argument(
            *(f"--{flag}" for flag in cheatsheet.section_flags(slug)),
            dest=slug,
            action="store_true",
            help=cheatsheet.section_help(slug),
        )
    cheat_views = cheat_p.add_mutually_exclusive_group()
    cheat_views.add_argument("--list", action="store_true", help="List section flags (default)")
    cheat_views.add_argument(
        "--all", action="store_true", help="Print the whole cheatsheet, overriding section flags"
    )


def _add_auth_subparser(sub) -> None:
    """Add the host credential-management command."""
    auth_p = sub.add_parser(
        "auth", help="Mint + store the agent's long-lived auth token (claude setup-token)"
    )
    auth_p.add_argument(
        "--app",
        choices=("claude", "codex"),
        default=None,
        help=(
            "AI agent client (credential changes default to claude; "
            "status uses the Project provider or both clients)"
        ),
    )
    auth_p.add_argument(
        "--status", action="store_true", help="Report which credential each agent would use"
    )
    auth_p.add_argument("--clear", action="store_true", help="Remove the stored credential")
    auth_p.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the credential from stdin instead of prompting",
    )


def _add_doctor_subparser(sub) -> None:
    """Add setup and environment diagnostics."""
    doctor_p = sub.add_parser(
        "doctor",
        help="Run setup and environment health checks [may invoke provider probe]",
        parents=[_project_root_parent("_cli_project_doctor")],
        description="Check setup and environment health. Deep checks may invoke a Specialist provider authorization and memory probe unless skipped.",
    )
    output = doctor_p.add_mutually_exclusive_group()
    output.add_argument("--verbose", "-v", action="store_true")
    output.add_argument(
        "--concise",
        action="store_true",
        help="Hide successful check rows while preserving findings and final counts",
    )
    doctor_p.add_argument(
        "--deep",
        action="store_true",
        help="Run real first-config sim/lint/synthesis smoke checks",
    )
    doctor_p.add_argument(
        "--skip-agent-checks",
        action="store_true",
        help=(
            "With --deep, skip agent credential inspection and the live "
            "Specialist provider authorization and memory probe"
        ),
    )


def _add_init_scaffold_arguments(init_p) -> None:
    """Add the optional new-IP scaffold controls to ``booley init``."""
    init_p.add_argument(
        "--scaffold",
        metavar="IP_NAME",
        help="Scaffold a new IP from scratch in a fresh repo: starter RTL, testbench, "
        ".core, and populated config, then run the regular init steps. Interactive "
        "wizard on a TTY; the flags below preset/replace its answers",
    )
    init_p.add_argument(
        "--sim-eda-tool",
        choices=("verilator", "icarus"),
        help="Scaffold simulator (default: verilator)",
    )
    init_p.add_argument("--sim-tool", action=_RetiredEdaToolOptionAction, help=argparse.SUPPRESS)
    init_p.add_argument(
        "--tb-style",
        choices=("sv", "cocotb"),
        help="Scaffold: testbench style (default sv; cocotb is sandbox-sim only)",
    )
    init_p.add_argument(
        "--lint-eda-tool",
        choices=("verilator", "verible"),
        help="Scaffold: lint EDA tool (default verilator --lint-only; verible = style lint)",
    )
    init_p.add_argument("--lint-tool", action=_RetiredEdaToolOptionAction, help=argparse.SUPPRESS)
    init_p.add_argument(
        "--asic",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scaffold: ASIC synthesis Target (default on; physical mode adds SDC)",
    )
    init_p.add_argument(
        "--synth-mode",
        choices=("logical", "physical"),
        help="Scaffold: synthesis mode (default physical; logical uses Yosys without SDC)",
    )
    init_p.add_argument(
        "--fpga-part",
        metavar="PART",
        help="Scaffold: enable fpga for this Vivado part (default off)",
    )


def _add_init_subparser(sub) -> None:
    """Add project initialization and Sandbox seeding."""
    init_p = sub.add_parser("init", help="Set up a new Booley project")
    init_p.add_argument(
        "--check-only", action="store_true", help="Run health checks without modifying anything"
    )
    init_p.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Skip agent credential inspection; provider and auth policy are still recorded",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Refresh Booley-managed host and Project resources; preserve user-owned files and caches",
    )
    init_p.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging output"
    )
    init_p.add_argument(
        "--fix-line-endings",
        action="store_true",
        help="Compatibility option; clean CRLF checkouts are repaired automatically",
    )
    init_p.add_argument(
        "--seed",
        action="store_true",
        help="Seed only the Interactive Mode devcontainer for this folder/worktree "
        "(no project scaffolding); run once per worktree",
    )
    init_p.add_argument(
        "--provider",
        choices=("claude", "codex"),
        help="Agent provider to record for this project (default: claude)",
    )
    init_p.add_argument(
        "--auth",
        type=_agent_auth_arg,
        metavar="{auto,subscription,api-key}",
        help="Agent authentication policy to record (default: auto)",
    )
    init_p.add_argument(
        "--repin-booley-dir",
        action="store_true",
        help="Re-pin a symlinked .booley entry to its current target",
    )
    _add_init_scaffold_arguments(init_p)


def _add_bootstrap_subparser(sub) -> None:
    """Add Project-independent Host Bootstrap."""
    parser = sub.add_parser("bootstrap", help="Prepare reusable Booley host resources")
    intent = parser.add_mutually_exclusive_group()
    intent.add_argument(
        "--check-only",
        action="store_true",
        help="Inspect Host Bootstrap readiness without modifying anything",
    )
    intent.add_argument(
        "--force",
        action="store_true",
        help="Refresh Booley-managed host resources even when they are current",
    )
    parser.add_argument(
        "--update",
        "--upgrade",
        dest="update",
        action="store_true",
        help="Update Host Bootstrap after upgrading Booley",
    )
    qa_skills = parser.add_mutually_exclusive_group()
    qa_skills.add_argument(
        "--with-qa-skills",
        action="store_true",
        help="Persist and reconcile maintainer QA skills from this primary source checkout",
    )
    qa_skills.add_argument(
        "--without-qa-skills",
        action="store_true",
        help="Disable QA skills and remove only Booley-managed QA links",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed reconciliation output"
    )


def _add_flow_subparser(sub) -> None:
    """Add direct Flow execution."""
    flow_p = sub.add_parser(
        "flow",
        help="Run a Booley Flow directly (e.g. `booley flow lint --target lint`)",
    )
    flow_p.add_argument(
        "endpoint_name",
        nargs="?",
        metavar="flow",
        help="Flow to run (omit to list the available Flows)",
    )
    flow_p.add_argument(
        "endpoint_args",
        nargs=argparse.REMAINDER,
        metavar="args",
        help="Arguments for the Flow; see `booley flow <flow> --help`",
    )


def _add_specialist_subparser(sub) -> None:
    """Add direct Specialist selection and verbatim endpoint arguments."""
    parser = sub.add_parser("specialist", help="Run a Specialist directly inside the Sandbox")
    parser.add_argument(
        "endpoint_name",
        nargs="?",
        metavar="specialist",
        help="Specialist to run (omit to list the available Specialists)",
    )
    parser.add_argument(
        "endpoint_args",
        nargs=argparse.REMAINDER,
        metavar="args",
        help="Arguments for the Specialist; see `booley specialist <specialist> --help`",
    )


def _add_session_subparser(sub) -> None:
    """Add lifecycle controls for the Sandbox."""
    from booley.flows.cli_arguments import add_project_root_alias

    root_opt = _project_root_parent("_cli_project_session_down")
    session_p = sub.add_parser(
        "session",
        help="Start/enter/stop the Sandbox container without VS Code",
    )
    session_sub = session_p.add_subparsers(
        dest="session_command",
        metavar="{up,enter,down,status,validate,refresh}",
    )
    up_p = session_sub.add_parser(
        "up",
        help="Create or start the Sandbox (default subcommand)",
    )
    up_p.add_argument(
        "--rebuild",
        action="store_true",
        help="Remove an existing container first, then recreate it",
    )
    enter_p = session_sub.add_parser(
        "enter",
        help="Open a shell in the Sandbox, or run `-- <cmd>` in it",
    )
    enter_p.add_argument(
        "exec_cmd",
        nargs=argparse.REMAINDER,
        help="Optional command, e.g. `booley session enter -- booley doctor`",
    )
    session_sub.add_parser("down", help="Stop and remove the Sandbox", parents=[root_opt])
    session_sub.add_parser("status", help="Print running/stopped/absent")
    session_sub.add_parser(
        "validate",
        help="Validate the host-issued Sandbox specification",
    )
    prepare_p = session_sub.add_parser("prepare")
    add_project_root_alias(prepare_p, "_cli_project_session_prepare")
    session_sub.add_parser(
        "refresh",
        help="Rebuild the configured image from current Booley sources and recreate the session",
    )


def _add_shell_subparser(sub) -> None:
    """Add the deliberately undocumented host debugging shell."""
    # `booley shell` is deliberately undocumented (no `help=`, hidden via the
    # subparsers metavar): a host-side debugging hatch, kept working but out
    # of the advertised Sandbox workflow (ADR 0028).
    shell_p = sub.add_parser(
        "shell",
        description="Open an interactive shell in a fresh sandbox container "
        "(worktree at /work); or run a one-off command with `-- <cmd>`",
    )
    shell_p.add_argument(
        "--net",
        action="store_true",
        help="Enable network egress (via the Booley proxy). Off by default: "
        "the shell runs offline, like the Sandbox.",
    )
    shell_p.add_argument(
        "shell_cmd",
        nargs=argparse.REMAINDER,
        help="Optional command to run non-interactively, e.g. "
        "`booley shell -- verilator --version`. Omit for an interactive shell.",
    )


def _add_utility_subparsers(sub) -> None:
    """Add host utilities, setup commands, and direct Flow commands."""
    sub.add_parser(
        "chat",
        help="Open this Project's configured agent CLI [invokes agent]",
        description="Open the configured interactive agent CLI for this Project.",
    )
    _add_cheat_subparser(sub)

    from booley.eda import cli as eda_cli

    eda_cli.add_subparser(sub)
    _add_auth_subparser(sub)
    _add_doctor_subparser(sub)
    _add_bootstrap_subparser(sub)
    _add_init_subparser(sub)
    _add_flow_subparser(sub)
    _add_specialist_subparser(sub)
    _add_targets_subparser(sub)

    # Skill adapters stay registered; top-level help advertises human commands only.
    feedback_cli.add_subparser(sub)
    cleanup_cli.add_subparser(sub)
    project_inventory_cli.add_subparser(sub)
    upgrade_cli.add_subparser(sub)
    _add_session_subparser(sub)
    _add_shell_subparser(sub)


def _add_goal_subparser(sub) -> None:
    """Add the Goal Mode group `booley goal` (ADR 0067 D13).

    Kept available to the Goal Mode skill without advertising it in top-level help.
    """
    goal_p = cast(
        "argparse.ArgumentParser",
        sub.add_parser(
            "goal",
            help="Inspect or abandon this worktree's Goal Mode; status shows every active Goal Mode in the Project when this worktree hosts none",
            description="Inspect this worktree's Goal Mode, or every active Goal Mode in the Project when this worktree hosts none. Abandon applies to this worktree's Goal Mode.",
        ),
    )
    goal_sub = goal_p.add_subparsers(
        dest="goal_command", metavar="{status,abandon}", required=True
    )
    status_help = (
        "Show this worktree's Goal Mode, or every active Goal Mode in the Project "
        "when this worktree hosts none."
    )
    status_p = goal_sub.add_parser("status", help=status_help, description=status_help)
    detail = status_p.add_mutually_exclusive_group()
    detail.add_argument("--short", action="store_true", help="One-line summary")
    detail.add_argument("--long", action="store_true", help="Full detail")
    abandon_p = goal_sub.add_parser("abandon", help="Abandon this worktree's Goal Mode")
    abandon_p.add_argument("--record-id", help="Exact expected Goal record for a retry")
    abandon_p.add_argument("--operation-id", help="Stable UUID operation for a retry")
    abandon_p.add_argument(
        "--instruction-quote",
        default="Human invoked booley goal abandon",
        help="Human instruction retained in the record",
    )


def _cmd_dashboard(args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness.dashboard.command import run_attach, run_dashboard

    if args.attach_policy or args.on_attach:
        return run_attach(project_root, policy_only=args.attach_policy)
    return run_dashboard(project_root)


def _goal_checkout(args: argparse.Namespace, project_root: Path) -> Path:
    """Use explicit CLI selection while retaining ambient checkout defaults."""
    return project_root if hasattr(args, "_cli_selection") else Path.cwd()


def _cmd_goal(args: argparse.Namespace, _project_root: Path) -> int:
    """Show Goal status using the Booley Directory discovered for this command."""
    if args.goal_command == "status":
        from booley.criteria.evidence_ledger import AcceptanceLedgerError
        from booley.goals.diagnostics import is_unnamable_error, unavailable_diagnostic
        from booley.goals.ended_records import with_last_ended
        from booley.goals.format import render_status
        from booley.goals.proposal_errors import ProposalError
        from booley.goals.state_store import GoalStateError
        from booley.goals.status import select_status
        from booley.goals.store import GoalStore, GoalStoreError
        from booley.runtime.booley_dir import resolve_booley_dir

        try:
            from booley.flows.sim.unpublished_results import read_unpublished_results

            store = GoalStore(resolve_booley_dir(_project_root))
            checkout = _goal_checkout(args, _project_root)
            selection = select_status(store, checkout, campaign_reader=read_unpublished_results)
            from booley.mcp.sim_baseline_jobs import schedule_goal_baselines

            for view in selection.views:
                schedule_goal_baselines(resolve_booley_dir(_project_root), view.record)
            short = True if args.short else False if args.long else None
            text = (
                render_status(selection.views, short=short, project_wide=selection.project_wide)
                if selection.views
                else "No active Goal Mode."
            )
            print(with_last_ended(text, store, checkout, project_wide=selection.project_wide))
            return 0
        except (GoalStoreError, GoalStateError, ProposalError, AcceptanceLedgerError) as exc:
            message = unavailable_diagnostic(exc) if is_unnamable_error(exc) else str(exc)
            print(f"ERROR: {message}", file=sys.stderr)
            return 2
    return _abandon_goal_cli(args, _project_root)


def _abandon_goal_cli(args: argparse.Namespace, _project_root: Path) -> int:
    """Render the shared lifecycle result and its exact retry identity."""
    from booley.goals.abandon import abandon_goal
    from booley.goals.checkout import CheckoutError
    from booley.goals.entry import EntryEnvironment
    from booley.goals.lifecycle import LifecycleError
    from booley.goals.proposals import ProposalError
    from booley.goals.store import GoalStore, GoalStoreError
    from booley.mcp.goal_completion import completion_environment
    from booley.runtime.booley_dir import resolve_booley_dir
    from booley.runtime.history_commit import FileCommitError

    try:
        booley_dir = resolve_booley_dir(_project_root)
        result = abandon_goal(
            GoalStore(booley_dir),
            _goal_checkout(args, _project_root),
            completion_environment(EntryEnvironment(booley_dir)),
            record_id=args.record_id,
            operation_id=args.operation_id,
            instruction_quote=args.instruction_quote,
            on_bound=lambda request: print(
                f"Retry IDs: --record-id {request.record_id} --operation-id {request.operation_id}",
                flush=True,
            ),
        )
        print(result["message"])
        return 0
    except (
        LifecycleError,
        GoalStoreError,
        ProposalError,
        CheckoutError,
        FileCommitError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _add_targets_subparser(sub) -> None:
    """Add the `booley targets` subparser (ADR 0030 Target listing)."""
    # The positional does double duty: a glob (contains * ? [) filters the
    # listing, anything else selects one Target for the resolved detail view.
    targets_p = sub.add_parser(
        "targets",
        help="List or filter .core Targets; full detail resolution requires the Sandbox",
    )
    targets_p.add_argument(
        "selector",
        nargs="?",
        default=None,
        metavar="NAME|GLOB",
        help="Target to detail (bare name or vlnv#name), or a glob like "
        "'soc*' to filter the listing",
    )
    # --for is the advertised spelling; --for-flow stays as a long-form alias.
    targets_p.add_argument(
        "--for",
        "--for-flow",
        dest="for_flow",
        metavar="FLOW",
        default=None,
        help="Only Targets this Booley Flow could drive (synth, fpga, sim, lint)",
    )
    targets_p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output (same data as the listing/detail view)",
    )


def _normalize_project_options(parser, args) -> None:
    from booley.flows.cli_selection import collect_project_selection

    collect_project_selection(parser, args)
    if args.command == "cleanup" and not hasattr(args, "project_root"):
        args.project_root = ""
    if hasattr(args, "_cli_selection") and (
        command_project_bindings()[args.command] is ProjectBinding.INDEPENDENT
    ):
        parser.error(f"--project is unsupported for {args.command}")
    _extract_endpoint_selection(parser, args)


def _normalize_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace:
    """Resolve the location-dependent default command."""
    if args.command is None:
        if not runtime_context.inside_session_runtime():
            parser.print_help()
            print(
                "\nGetting started: run `booley bootstrap`, then `booley init` in your RTL "
                "repo, and open the Project in its devcontainer. "
                f"See `booley --help` and {docs_url('SETUP.md')}"
            )
            parser.exit(0)
        else:
            args.command = "chat"
            args.default_chat = True

    _normalize_project_options(parser, args)
    _validate_doctor_args(parser, args)

    return args


def _validate_doctor_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Reject incomplete Doctor automation profiles at the CLI boundary."""
    if (
        args.command == "doctor"
        and getattr(args, "skip_agent_checks", False)
        and not getattr(args, "deep", False)
    ):
        parser.error("--skip-agent-checks requires --deep")


def _global_options_parser() -> argparse.ArgumentParser:
    """Read root options without actions that print help or inspect a Project."""
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--project", "-C")
    parser.add_argument("--help", "-h", "--version", action="store_true")
    return parser


def _argv_command(argv: list[str], globals_parser: argparse.ArgumentParser) -> str | None:
    """Find a command after a valid global prefix, leaving its entire tail opaque.

    Parse each candidate's prefix so variable-arity options cannot consume the
    command. A required option value must be supplied before that boundary.
    """
    commands = set(COMMAND_LOCATIONS) | set(RETIRED_COMMAND_POINTERS)
    for index, word in enumerate(argv):
        if word not in commands:
            continue
        prefix = argv[:index]
        if prefix and prefix[-1] == "--":
            prefix = prefix[:-1]
        try:
            options, unknown = globals_parser.parse_known_args(prefix)
        except argparse.ArgumentError:
            continue
        if not unknown:
            return None if options.help else word
    return None


def _parse_cli() -> argparse.Namespace:
    """Parse CLI args with subcommands."""
    _force_utf8()
    parser = _build_parser()
    command = _argv_command(sys.argv[1:], _global_options_parser())
    if command in RETIRED_COMMAND_POINTERS:
        parser.exit(2, RETIRED_COMMAND_POINTERS[command] + "\n")
    args = parser.parse_args()
    if args.command == "help":
        parser.print_help()
        parser.exit()
    return _normalize_args(parser, args)


def _live_criterion_endpoint_catalog(project_root: Path):
    """Compose the active project's Criteria with its discovered endpoints."""
    from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
    from booley.mcp.registry import (
        criterion_endpoint_relationships,
        discover_mcp_tools,
    )
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    booley_dir = resolve_checkout_booley_dir(project_root)
    return CriterionEndpointCatalog.load(
        booley_dir / "criteria.toml",
        criterion_endpoint_relationships(
            discover_mcp_tools(project_mcp_tools_dir=booley_dir / "mcp_tools")
        ),
    )


def _cheat_lists_sections(args: argparse.Namespace) -> bool:
    """Recognize the Project-independent section index, explicit or default."""
    return getattr(args, "list", False) or (
        not getattr(args, "all", False)
        and not any(getattr(args, slug, False) for slug in cheatsheet.section_slugs())
    )


def _cheat_flow_reference(text: str, project_root: Path | None) -> str:
    """Splice live Flow and Specialist catalogs, retaining committed fallbacks."""
    try:
        from booley.dev_support.flow_specialist_reference import (
            render_flow_reference,
            render_specialists_reference,
            splice_generated,
        )
        from booley.runtime.booley_dir import resolve_checkout_booley_dir

        # No Execution column: keep the terminal table within a terminal width
        # — a 4th column overflowed and truncated the Sets column (QA_REPORT
        # A3). Its reader is an agent, which doesn't pick execution backends
        # anyway; the docs blocks carry the matrix.
        if project_root is None:
            raise ValueError("No selected Project for live reference")
        project_mcp_tools_dir = resolve_checkout_booley_dir(project_root) / "mcp_tools"
        body = render_flow_reference(
            project_mcp_tools_dir=project_mcp_tools_dir,
            execution_column=False,
        )
        text = splice_generated(text, body)
        text = splice_generated(
            text,
            render_specialists_reference(project_mcp_tools_dir=project_mcp_tools_dir),
            name="specialists",
        )
    except (
        Exception  # noqa: BLE001 — best-effort live splice; fall back to committed block
    ):
        # Markers absent, registry unavailable, or optional deps missing:
        # fall back to the committed block so `booley cheat` always renders.
        pass
    return text


def _cheat_criteria_reference(text: str, project_root: Path | None) -> str:
    """Splice the active Project's Criteria and endpoint relationships."""
    try:
        from booley.criteria.reference import (
            splice_generated as splice_criteria,
        )
        from booley.goals.reference import render_goal_reference as render_criteria_reference
        from booley.runtime.booley_dir import resolve_checkout_booley_dir

        if project_root is None:
            raise ValueError("No selected Project for live Criteria")
        project_criteria = resolve_checkout_booley_dir(project_root) / "criteria.toml"
        text = splice_criteria(
            text,
            render_criteria_reference(
                _live_criterion_endpoint_catalog(project_root),
                project_criteria_path=project_criteria,
            ),
            name="criteria",
        )
    except Exception:  # noqa: BLE001 — best-effort live criteria splice; committed block remains on failure
        pass
    return text


def _cheat_criteria_params_reference(text: str) -> str:
    """Splice threshold flavours from the parameter registry."""
    try:
        from booley.criteria.reference import (
            render_criteria_params_reference,
        )
        from booley.criteria.reference import (
            splice_generated as splice_criteria,
        )

        text = splice_criteria(
            text,
            render_criteria_params_reference(),
            name="criteria-params",
        )
    except Exception:  # noqa: BLE001 — best-effort live splice; committed block remains on failure
        pass
    return text


def _cmd_cheat(args: argparse.Namespace, project_root: Path | None) -> int:
    if _cheat_lists_sections(args):
        for slug in cheatsheet.section_slugs():
            aliases = cheatsheet.section_flags(slug)[1:]
            alias_names = ", ".join(f"--{name}" for name in aliases)
            alias_note = f" (alias: {alias_names})" if aliases else ""
            print(
                f"  --{cheatsheet.section_flags(slug)[0]:<14}{cheatsheet.section_help(slug)}{alias_note}"
            )
        print(
            "\nExample: booley cheat --commands --project-files\n"
            "Use booley cheat --all for the whole cheatsheet."
        )
        return 0

    cs = cheatsheet_path()
    if not cs.exists():
        print("cheatsheet not found", file=sys.stderr)
        return 1
    text = cs.read_text(encoding="utf-8")
    text = _cheat_flow_reference(text, project_root)
    text = _cheat_criteria_reference(text, project_root)
    text = _cheat_criteria_params_reference(text)

    # Narrow to the requested sections (after splicing, so a filtered view still
    # shows live registry/criteria tables). --all keeps the whole sheet.
    text = cheatsheet.select(
        text,
        []
        if getattr(args, "all", False)
        else [slug for slug in cheatsheet.section_slugs() if getattr(args, slug, False)],
    )

    # Drop the HTML-comment markers so they don't reach the terminal.
    text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("<!--"))

    print(render(text), end="")
    return 0


def _report_session_health(
    project_root: Path, *, startup_due_reason: str | None = None, started: bool = True
) -> None:
    """Surface the result, or the scheduled check, after Sandbox start."""
    from booley.runtime import health, health_presentation

    due_reason = startup_due_reason or health.due_reason(project_root)
    if due_reason is not None:
        timing = "from before startup " if started else ""
        print(
            f"Automatic Doctor is {'running' if started else 'due'} in the Sandbox ({due_reason}); "
            f"persisted findings {timing}will not be reported as current.",
            file=sys.stderr,
        )
        return
    snapshot = health.consume_changed_snapshot(project_root, channel="session-up")
    if snapshot is not None:
        prefix = "" if snapshot.assessment.clean else "warning: "
        print(f"{prefix}{health_presentation.brief_message(snapshot)}", file=sys.stderr)
        return


def _session_up(args: argparse.Namespace, project_root: Path) -> int:
    """Create or resume the headless Sandbox."""
    from booley.runtime import health
    from booley.runtime import session_runtime as sr

    _report_upgrade_before_session(project_root)
    startup_due_reason = health.due_reason(project_root)
    result = sr.ensure(project_root, rebuild=getattr(args, "rebuild", False))
    name = result.name
    if result.vscode:
        print(f"Reusing Sandbox owned by VS Code: {name}")
    _report_session_health(
        project_root, startup_due_reason=startup_due_reason, started=result.started
    )
    from booley.harness.doctor import observe_deep_status

    print(observe_deep_status(project_root, container=name).render())
    print(f"Sandbox ready: {name}")
    print("  enter it with: booley session enter")
    return 0


def _report_upgrade_before_session(project_root: Path) -> None:
    """Observe host version state and advise before starting a Sandbox."""
    try:
        from booley.runtime.booley_dir import resolve_checkout_booley_dir

        status = upgrade_review.observe(resolve_checkout_booley_dir(project_root))
    except Exception:  # noqa: BLE001 — advisory state must never block Session startup
        return
    if status.condition is upgrade_review.ReviewCondition.CURRENT:
        return
    print(f"warning: {upgrade_cli.render_status(status)}", file=sys.stderr)


def _session_refresh(args: argparse.Namespace, project_root: Path) -> int:
    """Reconcile the Sandbox Image and replace its Sandbox."""
    configure_progress_output()
    from booley.harness.session_refresh import refresh
    from booley.runtime import health

    verbose = getattr(args, "verbose", False)
    result = refresh(project_root, verbose=verbose)
    _report_session_health(
        project_root,
        startup_due_reason=health.due_reason(project_root),
    )
    print(result.message)
    return 0


def _session_enter(args: argparse.Namespace, project_root: Path) -> int:
    from booley.runtime import session_runtime as sr

    raw = list(getattr(args, "exec_cmd", []) or [])
    if raw and raw[0] == "--":
        raw = raw[1:]
    return sr.enter(
        project_root,
        raw or None,
        tty=sys.stdin.isatty() and sys.stdout.isatty(),
        cwd=getattr(args, "_session_cwd", None),
    )


def _session_down(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.runtime import session_runtime as sr

    result = sr.down(project_root)
    if result:
        details = []
        if result.headless:
            details.append(f"removed {sr.session_container_name(project_root)}")
        if result.claim_cleared:
            details.append("cleared pending editor start claim")
        print("; ".join(details) or "removed Sandbox resources")
        if result.claim_cleared:
            print(
                "Cancel any in-progress VS Code create before clearing its claim.", file=sys.stderr
            )
    else:
        print("no Sandbox container for this Project")
    return 0


def _session_status(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.runtime import session_runtime as sr

    print(sr.status(project_root))
    return 0


def _session_validate(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.runtime import session_runtime as sr

    print(sr.validate(project_root))
    return 0


def _session_prepare(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.runtime import session_runtime as sr

    print(sr.prepare(project_root))
    return 0


def _cmd_session(args: argparse.Namespace, project_root: Path) -> int:
    """Drive the Sandbox container headlessly (no VS Code, no UI)."""
    from booley.runtime import session_runtime as sr
    from booley.runtime.docker_capacity import DockerCapacityError

    handlers: dict[str, Callable[[argparse.Namespace, Path], int]] = {
        "up": _session_up,
        "enter": _session_enter,
        "down": _session_down,
        "status": _session_status,
        "validate": _session_validate,
        "prepare": _session_prepare,
        "refresh": _session_refresh,
    }
    sub = getattr(args, "session_command", None) or "up"
    handler = handlers.get(sub)
    if handler is None:
        print(f"ERROR: unknown session subcommand {sub!r}", file=sys.stderr)
        return 2
    try:
        return handler(args, project_root)
    except DockerCapacityError as exc:
        print(f"[XX] image-capacity: {exc}", file=sys.stderr)
        return 2
    except sr.SessionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ERROR: docker not found on PATH.", file=sys.stderr)
        return 2
    except LifecycleLockError:
        raise
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _cmd_shell(args: argparse.Namespace, project_root: Path) -> int:
    """Open an interactive shell (or run a one-off command) in a fresh sandbox.

    A **host** command by design: it spawns a throwaway `booley-sandbox`
    container with the worktree bind-mounted at `/work` and the same security /
    resource flags as the MCP-tool-call path, so a host-side agent (or a person) can
    poke at the toolchain during setup without Reopen-in-Container. It cannot run
    inside the sandbox — the container has no Docker access (ADR 0016).
    """
    if runtime_context.inside_session_runtime():
        print(
            "ERROR: `booley shell` cannot run inside the Booley container.\n\n"
            "  The sandbox has no Docker access by design (ADR 0016), so it "
            "cannot spawn\n  another container. You are already inside a sandbox "
            "— just use this shell.\n"
            "  Run `booley shell` from a HOST terminal to get a fresh sandbox.",
            file=sys.stderr,
        )
        return 2

    from booley.config.settings import get_agent_settings, load_agent_settings
    from booley.harness.sandbox import DockerRunner, DockerSandboxConfig

    # Load the project's booley.toml ([sandbox].image, memory, ...) — without
    # this the lazy default config silently spawns the base image even when
    # the project configured a custom one. Early setup (no booley.toml yet)
    # still gets a shell on the defaults.
    try:
        load_agent_settings(project_root)
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError covers BackendConfigError (invalid [agent] provider /
        # sandbox mode) — mirror doctor, which warns-and-defaults rather than
        # tracebacking a `booley shell` on a fixable toml typo.
        print(
            f"warning: could not load project config ({exc}); using sandbox defaults",
            file=sys.stderr,
        )

    cfg = get_agent_settings()
    docker_cfg = DockerSandboxConfig(
        image=cfg.sandbox.image,
        needs_network=bool(getattr(args, "net", False)),
        # Generous ceiling: this is a human/agent poking at real EDA tools,
        # not a metered MCP tool call. A project that declared its own
        # [sandbox].memory (e.g. a 415K-LOC core whose sv2v pass brushes 4g)
        # gets that limit here too — the shell must not be tighter than the
        # Sandbox the same Flows and Specialists normally run in.
        memory_limit=cfg.sandbox.memory or "4g",
    )
    error = docker_cfg.verify()
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    # Strip an optional leading `--` separator: `booley shell -- cmd` and
    # `booley shell cmd` both mean "run cmd".
    raw = list(getattr(args, "shell_cmd", []) or [])
    if raw and raw[0] == "--":
        raw = raw[1:]

    is_interactive = not raw
    payload = raw if raw else ["/bin/bash", "-l"]
    # Only allocate a TTY when both ends are real terminals; `-t` on a pipe
    # (e.g. an agent capturing output) fails with "the input device is not a TTY".
    tty = is_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    runner = DockerRunner(docker_cfg, project_root, label="shell")
    argv = runner.ephemeral_argv(payload, tty=tty)
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        print("ERROR: docker not found on PATH.", file=sys.stderr)
        return 2
    finally:
        runner.cleanup_ephemeral()


def _discover_project_mcp_tools(project_root: Path) -> list[McpToolInfo]:
    """All MCP endpoints visible to the diagnostic CLI commands.

    Discovery is deliberately unfiltered so the diagnostic commands work while
    a project is being configured.
    """
    from booley.mcp.registry import discover_mcp_tools

    return discover_mcp_tools(project_mcp_tools_dir=project_root / BOOLEY_DIR_NAME / "mcp_tools")


def _load_mcp_tool_class(info: McpToolInfo) -> type | None:
    """Import the module behind a discovered endpoint.

    Built-in paths include their canonical package. Project-defined endpoints
    are absolute files below ``.booley/mcp_tools``.
    """
    import importlib
    import importlib.util

    from booley.flows.base import BooleyFlow, BuiltinFlow
    from booley.mcp.base import McpTool

    path = Path(info.path)
    if path.is_absolute():
        spec = importlib.util.spec_from_file_location(f"booley_custom_tools.{path.stem}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module.__name__] = module
        spec.loader.exec_module(module)
    else:
        package = path.parent.as_posix().replace("/", ".")
        module = importlib.import_module(f"booley.{package}.{path.stem}")

    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, (McpTool, BooleyFlow, BuiltinFlow))
            and obj is not McpTool
            and getattr(obj, "name", "") == info.name
        ):
            return obj
    return None


_FLOW_BLURB_CHARS = 72


def _flow_blurb(description: str) -> str:
    """Return one terminal line from a Flow description.

    Endpoint descriptions can run to paragraphs; the CLI listing stays terse.
    """
    first = description.split(". ", maxsplit=1)[0].strip()
    if len(first) > _FLOW_BLURB_CHARS:
        first = first[: _FLOW_BLURB_CHARS - 1].rstrip() + "…"
    return first


def _flow_listing(flows: list[McpToolInfo]) -> str:
    """Render available Flows for the CLI."""
    if not flows:
        return "  (none discovered)"
    width = max(len(flow.name) for flow in flows)
    return "\n".join(
        f"  {flow.name:<{width}}  {_flow_blurb(flow.description)}"
        for flow in sorted(flows, key=lambda item: item.name)
    )


def _select_endpoint(args, endpoints: list[McpToolInfo], kind: str) -> McpToolInfo | int:
    """List or select a registered endpoint, with wrong-command guidance."""
    from booley.targets.flow_names import canonical

    choices = [item for item in endpoints if item.kind == kind]
    heading = "Flows" if kind == "flow" else "Specialists"
    raw_name = getattr(args, "endpoint_name", None)
    if raw_name is None:
        print(f"Available {heading}:\n{_flow_listing(choices)}")
        return 0
    name = canonical(raw_name) if kind == "flow" else raw_name
    info = next((item for item in choices if item.name == name), None)
    if info is not None:
        return info
    if _endpoint_help_candidate(args):
        _enforce_runtime_location(args.command)
    other = next((item for item in endpoints if item.name == canonical(raw_name)), None)
    suggestion = f" Use booley {other.kind} {other.name}." if other is not None else ""
    print(
        f"ERROR: {name!r} is not a {kind} for booley {kind}.{suggestion}"
        f"\n\nAvailable {heading}:\n{_flow_listing(choices)}",
        file=sys.stderr,
    )
    return 2


def _endpoint_help_candidate(args: argparse.Namespace) -> bool:
    """Recognize help-looking syntax; the real parser decides its meaning."""
    if args.command not in {"flow", "specialist"} or not args.endpoint_name:
        return False
    return any(
        word.startswith("--h")
        or (word.startswith("-") and not word.startswith("--") and "h" in word[1:])
        for word in args.endpoint_args
    )


def _dispatch_endpoint(args, info: McpToolInfo, *, flow: bool) -> int:
    """Dispatch a Flow or Specialist through its execution adapter."""
    try:
        endpoint_cls = _load_mcp_tool_class(info)
    except Exception as exc:  # noqa: BLE001 — report failures at the Project plugin boundary
        print(
            f"ERROR: could not load endpoint {info.name!r} from {info.path}: {exc}",
            file=sys.stderr,
        )
        return 2
    if endpoint_cls is None:
        print(f"ERROR: could not load endpoint {info.name!r} from {info.path}.", file=sys.stderr)
        return 2
    argv = list(getattr(args, "endpoint_args", []) or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    endpoint = endpoint_cls()
    if _endpoint_help_candidate(args):
        from booley.harness.endpoint_help import supports_shared_cli, try_render_help

        if supports_shared_cli(endpoint):
            try_render_help(endpoint, argv, getattr(args, "_cli_endpoint_project", None))
        elif not runtime_context.inside_session_runtime():
            print(
                "ERROR: host help requires the shared CLI; inspect this custom command inside the Sandbox.",
                file=sys.stderr,
            )
            return 2
        _enforce_runtime_location(args.command)
        runtime_context.ensure_proxy_env()
    return _invoke_endpoint(endpoint, argv, args)


def _cmd_flow(args: argparse.Namespace, project_root: Path) -> int:
    """Run a deterministic Flow through its existing entry point."""
    info = _select_endpoint(args, _discover_project_mcp_tools(project_root), "flow")
    return info if isinstance(info, int) else _dispatch_endpoint(args, info, flow=True)


def _cmd_specialist(args: argparse.Namespace, project_root: Path) -> int:
    """Run a visible Specialist through its existing entry point."""
    from booley.mcp.endpoint_config import get_endpoint_config
    from booley.mcp.registry import discover_mcp_tools
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    config, flows = get_endpoint_config(project_root)
    try:
        project_tools = resolve_checkout_booley_dir(project_root) / "mcp_tools"
    except FileNotFoundError:
        project_tools = None
    endpoints = discover_mcp_tools(
        project_mcp_tools_dir=project_tools,
        specialist_config=config,
        flow_config=flows,
    )
    info = _select_endpoint(args, endpoints, "specialist")
    return info if isinstance(info, int) else _dispatch_endpoint(args, info, flow=False)


def _target_detail_payload(
    project_root: Path, selector: str, *, as_json: bool
) -> dict[str, object]:
    """Resolve Target detail in-Sandbox, or return actionable host metadata."""
    from booley.targets import target_surface

    inside_runtime = runtime_context.inside_session_runtime()
    payload = target_surface.detail_payload(project_root, selector, resolve=inside_runtime)
    if inside_runtime:
        return payload
    command = ["booley", "session", "enter", "--", "booley", "targets", selector]
    if as_json:
        command.append("--json")
    payload["resolved_error"] = "detailed Target resolution requires the Sandbox"
    payload["resolution_command"] = shlex.join(command)
    return payload


def _cmd_targets(args: argparse.Namespace, project_root: Path) -> int:
    """List the project's ``.core`` Targets, or detail one: `booley targets`.

    Pure ``.core``-YAML enumeration works on either side of the Sandbox
    boundary. Single-Target resolution runs only inside the Sandbox;
    host detail degrades to the cheap half with an entry command.
    """
    import json as _json

    from booley.fusesoc import fusesoc_registry
    from booley.targets import target_surface

    selector: str | None = getattr(args, "selector", None)
    for_flow: str | None = getattr(args, "for_flow", None)
    if for_flow:
        from booley.targets.flow_names import canonical

        for_flow = canonical(for_flow)
    as_json: bool = getattr(args, "json", False)

    if selector and not target_surface.is_glob(selector):
        # Detail view. --for is a listing filter — combining it with a single
        # Target would silently answer a different question, so refuse.
        if for_flow:
            print(
                "ERROR: --for filters the listing; it cannot combine with a "
                "single-Target detail view.",
                file=sys.stderr,
            )
            return 2
        try:
            payload = _target_detail_payload(project_root, selector, as_json=as_json)
        except fusesoc_registry.FuseSocError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(_json.dumps(payload, indent=2) if as_json else target_surface.render_detail(payload))
        return 0

    try:
        surface = target_surface.collect_surface(project_root)
        nothing_authored = not surface.groups
        surface = target_surface.filter_surface(surface, for_flow=for_flow, glob=selector)
    except fusesoc_registry.FuseSocError as exc:  # e.g. cross-root VLNV collision
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # --for names a non-Target-aware endpoint
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(_json.dumps(target_surface.surface_payload(surface, project_root), indent=2))
    elif nothing_authored:
        print("no .core Targets authored yet")
    else:
        print(target_surface.render_listing(surface, project_root))
    return 0


def _cmd_feedback(args: argparse.Namespace, project_root: Path) -> int:
    """Compose Doctor state only for Feedback operations that render it."""
    env = None
    if feedback_cli.requires_environment(args):
        env = resolve_feedback_environment(feedback_storage_dir(project_root))
    return feedback_cli.run(args, project_root, env=env)


_EARLY_COMMANDS: dict[str, Callable] = {
    "chat": run_chat,
    "cheat": _cmd_cheat,
    "doctor": run_doctor,
    "init": run_init,
    "auth": run_auth,
    "shell": _cmd_shell,
    "session": _cmd_session,
    "targets": _cmd_targets,
    "worktree": run_worktree,
    "goal": _cmd_goal,
    "dashboard": _cmd_dashboard,
    "flow": _cmd_flow,
    "specialist": _cmd_specialist,
    "feedback": _cmd_feedback,
    "cleanup": cleanup_cli.run,
    "upgrade": upgrade_cli.run,
}

from booley.eda import cli as _eda_cli


def _cmd_eda(args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness.eda_grants import COORDINATOR

    return _eda_cli.run(args, project_root, grant_mutator=COORDINATOR)


_EARLY_COMMANDS["eda"] = _cmd_eda


def _handle_early_exits(args: argparse.Namespace, project_root: Path) -> int | None:
    """Dispatch non-run subcommands; returns exit code or None for 'run'."""
    handler = _EARLY_COMMANDS.get(args.command)
    if handler is not None:
        return handler(args, project_root)
    return None


def _reject_source_project_command(command: str, project_root: Path) -> int | None:
    """Reject Project commands in Booley source while allowing dogfood feedback."""
    if command_project_bindings()[command] in {
        ProjectBinding.INDEPENDENT,
        ProjectBinding.OPTIONAL,
        ProjectBinding.FEEDBACK,
    }:
        return None
    from booley.runtime.checkout_role import SourceCheckoutProjectError, require_project_checkout

    try:
        require_project_checkout(project_root)
    except SourceCheckoutProjectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


# Runtime-location table (ADR 0028 Decision 2): where each command may run.
# Container-only commands ARE the Booley workflow — they act on the Session
# Runtime. Host-only commands create/serve the container boundary itself.
# `doctor` is dual (context-aware checks inside); `cheat` runs anywhere.
# `shell` needs host Docker too, but keeps its own tailored refusal in
# _cmd_shell ("you are already inside a sandbox — just use this shell").
_CONTAINER_ONLY_COMMANDS = frozenset(
    command
    for command, location in COMMAND_LOCATIONS.items()
    if location is CommandLocation.SESSION_RUNTIME
)
# `session` drives the Sandbox from outside it: like `init` it needs host
# Docker, and the sandbox has none (ADR 0016).
_HOST_ONLY_COMMANDS = frozenset(
    command for command, location in COMMAND_LOCATIONS.items() if location is CommandLocation.HOST
)


def _effective_command(args: argparse.Namespace) -> str | None:
    """Return the command supplied by CLI normalization, if any."""
    return str(args.command) if args.command else None


def _enforce_runtime_location(command: str | None) -> None:
    """Refuse a command invoked on the wrong side of the container boundary.

    One chokepoint before runtime setup (ADR 0028): Project validation may
    reject explicit Project-bound commands first. The venue refusal names
    the fix and precedes runtime mutation.
    """
    if command is None:
        return
    error: str | None = None
    location = COMMAND_LOCATIONS.get(command)
    if location is CommandLocation.SESSION_RUNTIME:
        argv = ["booley", *sys.argv[1:]]
        invocation = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        error = runtime_context.container_only_error(invocation)
    elif location is CommandLocation.HOST:
        error = runtime_context.host_only_error(f"booley {command}")
    if error is not None:
        print(error, file=sys.stderr)
        sys.exit(2)


def _host_install_authority_error(command: str | None) -> str | None:
    """Reject host lifecycle commands imported from a noncanonical installation."""
    if command not in _HOST_ONLY_COMMANDS or command == "bootstrap":
        return None
    from booley.runtime.host_install import host_install_error
    from booley.runtime.paths import skills_dir

    return host_install_error(skills_dir())


def _command_project_root(command: str) -> Path:
    """Feedback retains its explicitly supported source-checkout routing."""
    from booley.core.checkout_role import is_booley_source_checkout
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    if command in {"init", "doctor"}:
        # Init/Doctor must reach their layout boundary even for an old-only Project.
        current = Path.cwd().resolve()
        for parent in (current, *current.parents):
            if (parent / ".git").exists():
                return parent
        return current
    try:
        return find_project_root()
    except ProjectRootDiscoveryError:
        if command == "feedback":
            current = Path.cwd().resolve()
            for parent in (current, *current.parents):
                if is_booley_source_checkout(parent):
                    return parent
        raise


class ProjectRequiredError(RuntimeError):
    """A Project-bound CLI command has no initialized Project context."""


def _require_command_project(command: str, project_root: Path) -> None:
    """Validate required Project context before dispatch, allowing initialization."""
    from booley.core.booley_dir_pin import BooleyDirPinError, entry_needs_pin
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    # Initialization creates the context this guard requires, including in non-Git folders.
    if command == "init":
        return
    if command == "doctor":
        from booley.core.booley_layout import require_current_layout

        require_current_layout(project_root)
    try:
        with warnings.catch_warnings():
            # Replace the stale-selection warning with the CLI's actionable error.
            warnings.filterwarnings(
                "ignore",
                message=r"BOOLEY_DIR=.*does not exist; using anyway",
                category=UserWarning,
            )
            present = resolve_checkout_booley_dir(project_root).is_dir()
    except FileNotFoundError:
        present = False
    except PermissionError as exc:
        raise ProjectRequiredError(f"cannot access Booley Directory: {exc}") from exc
    except BooleyDirPinError:
        # Doctor reports the checkout's own untrusted entry as a FAIL; an ancestor's
        # untrusted link is not Doctor's to report, so it refuses like any command.
        if command != "doctor" or not entry_needs_pin(project_root):
            raise
        present = True
    if not present:
        raise ProjectRequiredError(
            "not a Booley Project; cd into one or run `booley init` on the host."
        )


def _optional_project_root(args: argparse.Namespace) -> Path | None:
    from booley.core.checkout_role import SourceCheckoutProjectError, require_project_checkout
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    if getattr(args, "command", None) == "cheat" and _cheat_lists_sections(args):
        return None
    try:
        root = (
            _resolve_cli_selection(args)
            if hasattr(args, "_cli_selection")
            else Path(args.project_root).resolve()
            if getattr(args, "project_root", None)
            else find_project_root()
        )
        root = require_project_checkout(root)
        from booley.runtime.booley_dir import resolve_checkout_booley_dir

        booley_dir = resolve_checkout_booley_dir(root)
        require_project_checkout(booley_dir)
        require_project_checkout(booley_dir / "mcp_tools")
        return root
    except (
        ProjectRootDiscoveryError,
        SourceCheckoutProjectError,
        FileNotFoundError,
        PermissionError,
    ) as exc:
        if hasattr(args, "_cli_selection"):
            raise ProjectRootDiscoveryError(str(exc)) from exc
        return None


def _auth_project_root(args: argparse.Namespace) -> Path | None:
    """Keep optional auth authority within the selected checkout's own state."""
    from booley.runtime.booley_dir import resolve_authoritative_booley_dir
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    root = _optional_project_root(args)
    if root is None:
        return None
    for candidate in (root, *root.parents):
        own_state = (candidate / ".booley").is_dir() or (candidate / "booley.toml").is_file()
        if own_state and resolve_authoritative_booley_dir(candidate).is_dir():
            return candidate
        if (candidate / ".git").exists():
            break
    if hasattr(args, "_cli_selection") or getattr(args, "project_root", None):
        from booley.runtime.project_discovery import linked_auth_error

        # A linked worktree needs the Project Root, not `booley init`.
        raise ProjectRootDiscoveryError(
            linked_auth_error(root)
            or "not a Booley Project; cd into one or run `booley init` on the host."
        )
    return None


def _selected_project_root(
    command: str, args: argparse.Namespace, binding: ProjectBinding
) -> Path | None:
    """Select context without discovering Projects for independent commands."""
    if binding is ProjectBinding.INDEPENDENT:
        return Path.cwd()
    if binding is ProjectBinding.OPTIONAL:
        return _auth_project_root(args) if command == "auth" else _optional_project_root(args)
    if hasattr(args, "_cli_selection"):
        return _resolve_cli_selection(args)
    if getattr(args, "project_root", None):
        return Path(args.project_root).resolve()
    return _command_project_root(command)


def _host_session_selection(
    command: str, args: argparse.Namespace, selected: Path | None
) -> Path | None:
    from booley.runtime.project_discovery import (
        ProjectRootDiscoveryError,
        discover_session_context,
        linked_auth_error,
    )

    if (
        command == "auth"
        and not getattr(args, "status", False)
        and (message := linked_auth_error(selected))
    ):
        raise ProjectRequiredError(message)
    if command != "session":
        return selected
    explicit = (
        hasattr(args, "_cli_selection")
        or bool(getattr(args, "project_root", None))
        or bool(os.environ.get("RTL_PROJECT_ROOT"))
    )
    context = discover_session_context(selected, invocation_cwd=None if explicit else Path.cwd())
    sub = getattr(args, "session_command", None) or "up"
    if context.checkout != context.project_root:
        if sub == "prepare":
            raise ProjectRootDiscoveryError(
                "Cannot prepare a linked worktree folder; run from the Project Root."
            )
        if sub == "enter" and context.cwd is None:
            raise ProjectRootDiscoveryError(
                "Linked worktree is outside the issued Sandbox mounts; run from the Project Root."
            )
        args._session_cwd = context.cwd
    return context.project_root


def _dispatch_main() -> int:
    """Parse CLI, handle early exits, and dispatch the selected command."""
    args = _parse_cli()
    command = _effective_command(args)

    default_chat = getattr(args, "default_chat", False)
    if default_chat:
        _enforce_runtime_location(command)

    binding = _command_project_binding(command, args)
    project_root = _selected_project_root(command, args, binding)
    if binding not in {ProjectBinding.INDEPENDENT, ProjectBinding.OPTIONAL}:
        source_rejection = _reject_source_project_command(command, project_root)
        if source_rejection is not None:
            return source_rejection

    if command in {"session", "auth"} and not runtime_context.inside_session_runtime():
        project_root = _host_session_selection(command, args, project_root)

    if binding is ProjectBinding.REQUIRED:
        _require_command_project(command, project_root)

    return _dispatch_selected(args, command, project_root, binding)


def _dispatch_selected(
    args: argparse.Namespace, command: str, project_root: Path | None, binding: ProjectBinding
) -> int:
    """Dispatch an already validated selection under its host consumer admission."""
    default_chat = getattr(args, "default_chat", False)
    if _endpoint_help_candidate(args):
        return _dispatch_help_candidate(args, project_root)
    # Bootstrap has no Project and must not even discover one. Its host-only
    # venue guard still runs before configuration or reconciliation.
    if not default_chat:
        _enforce_runtime_location(command)
    if authority_error := _host_install_authority_error(command):
        print(authority_error, file=sys.stderr)
        return 2
    if command == "bootstrap":
        return run_bootstrap(args)
    if command == "projects":
        from booley.runtime import image_keepers as runtime_image_keepers

        return project_inventory_cli.run(args, keeper_operations=runtime_image_keepers)

    # Project and runtime-location guards precede runtime mutation and screen clearing.
    # docker-exec entry drops the spec's remoteEnv — self-heal the proxy env
    # here so agents spawned below inherit a working egress path.
    try:
        if runtime_context.ensure_proxy_env():
            logger.debug("proxy env was absent in-container — defaulted to booley-proxy")
    except runtime_context.EgressModeError:
        # Doctor must still run to report the invalid declaration as a FAIL.
        if command != "doctor":
            raise

    if _outer_endpoint_selection(args) and command in {"flow", "specialist"}:
        args._cli_endpoint_project = project_root
    from booley.runtime.layout_consumers import directory_consumer

    guarded = binding is ProjectBinding.REQUIRED and command not in {"init"}
    scope = (
        directory_consumer(project_root) if guarded and project_root is not None else nullcontext()
    )
    with scope:
        early = _handle_early_exits(args, project_root)
    if early is not None:
        return early
    raise AssertionError(f"unhandled command: {command}")


def _dispatch_help_candidate(args: argparse.Namespace, project_root: Path) -> int:
    """Keep metadata admission while avoiding execution-path runtime setup."""
    from booley.runtime.layout_consumers import directory_consumer

    if _outer_endpoint_selection(args):
        args._cli_endpoint_project = project_root
    with directory_consumer(project_root):
        result = _handle_early_exits(args, project_root)
    assert result is not None, "endpoint help command has no handler"
    return result


def main() -> int:
    """Run the CLI with one rendering boundary for lifecycle contention."""
    from booley.core.booley_layout import BooleyLayoutError
    from booley.mcp.endpoint_config import EndpointConfigError
    from booley.runtime.checkout_role import SourceCheckoutProjectError
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    try:
        return _dispatch_main()
    except (
        LifecycleLockError,
        BooleyLayoutError,
        ProjectRootDiscoveryError,
        EndpointConfigError,
        ProjectRequiredError,
        SourceCheckoutProjectError,
        runtime_context.EgressModeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
