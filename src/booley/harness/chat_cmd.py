"""Launch the Agent CLI the current Project allows: its default, or the one `--cli` names."""

from __future__ import annotations

import argparse
import functools
import os
import shutil
import sys
from pathlib import Path

from booley.config.agent import AgentSettings
from booley.config.gateway import GatewayConfigError
from booley.config.settings import BackendConfigError, get_agent_settings, load_agent_settings
from booley.harness import codex_chat
from booley.runtime.booley_dir import resolve_checkout_booley_dir, resolve_common_booley_dir
from booley.runtime.foreground_child import run_foreground_child
from booley.runtime.handles import reserve_session_handle
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_activity import process_session_key


def _names_session(agent_args: list[str]) -> bool:
    """Return whether the user's own arguments (before any `--`) already name the session."""
    for token in agent_args:
        if token == "--":
            return False
        if token in ("-n", "--name") or token.startswith("--name="):
            return True
    return False


def _claude_argv(project_root: Path, agent_args: list[str]) -> list[str]:
    """Use the launch process's persistent handle when naming is available.

    The handle stays argv[0]; Booley adds `-n <handle>` unless the user named the session.
    """
    try:
        identity = capture_process_identity(os.getpid())
        if identity is None:
            raise RuntimeError("launch process identity is unavailable")
        handle = reserve_session_handle(
            resolve_common_booley_dir(project_root), process_session_key(identity)
        )
        return [handle] if _names_session(agent_args) else [handle, "-n", handle]
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"NOTE: Claude tab handle is unavailable: {exc}", file=sys.stderr)
        return ["claude"]


def _cli_not_found(provider: str) -> int:
    """Report that the configured agent CLI is absent and return the exit code."""
    print(f"ERROR: configured agent CLI '{provider}' was not found on PATH.", file=sys.stderr)
    return 2


def _selected_cli(requested: str | None, settings: AgentSettings) -> str | None:
    """Return the CLI to launch, or None after reporting a `--cli` the Project forbids.

    No request means the default CLI, so the flagless launch never reads `clis`.
    """
    if requested is None:
        return settings.default_cli
    if requested in settings.clis:
        return requested
    print(
        f"ERROR: Agent CLI '{requested}' is not allowed in this Project; "
        f"allowed: {', '.join(settings.clis)} (booley.toml [agent] clis).",
        file=sys.stderr,
    )
    return None


def _launch(provider: str, project_root: Path, agent_args: list[str]) -> int:
    """Exec the agent CLI; runs in the forked child, so identities are the agent's own.

    Returns an exit code only when the launch fails: on success the process is replaced.
    """
    try:
        executable, argv = provider, [provider]
        if provider == "codex":
            executable, argv = codex_chat.command(project_root)
        elif provider == "claude":
            argv = _claude_argv(project_root, agent_args)
        # Last statement before the process is replaced: the CLI exists and the
        # launch arguments are settled, so the notice only precedes a real start.
        print(f"Booley is starting the {provider} interactive agent.", file=sys.stderr)
        os.execvp(executable, [*argv, *agent_args])
    except FileNotFoundError:
        return _cli_not_found(provider)
    except OSError as exc:
        print(f"ERROR: could not launch agent CLI '{provider}': {exc}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Run the Project's default (or `--cli`-chosen) interactive Agent CLI as a foreground child."""
    try:
        booley_dir = resolve_checkout_booley_dir(project_root)
        load_agent_settings(project_root, booley_dir=booley_dir)
        # Bare `booley` builds its namespace without chat options, so `cli` may be absent.
        provider = _selected_cli(getattr(args, "cli", None), get_agent_settings())
        if provider is None:
            return 2
    except GatewayConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (BackendConfigError, FileNotFoundError) as exc:
        print(f"ERROR: could not resolve the Project's agent provider: {exc}", file=sys.stderr)
        return 2

    # Existence only (F_OK): a non-executable file keeps the launch error below.
    if shutil.which(provider, mode=os.F_OK) is None:
        return _cli_not_found(provider)

    # The child, not Booley, reserves the handle: the handle belongs to the agent's process.
    return run_foreground_child(
        functools.partial(_launch, provider, project_root, args.agent_args)
    )
