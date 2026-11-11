"""Launch the Agent CLI the current Project allows: its default, or the one `--cli` names."""

from __future__ import annotations

import argparse
import contextlib
import functools
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from booley.config.agent import AgentSettings, issued_codex_gateway
from booley.config.chat import chat_worktree
from booley.config.gateway import GATEWAY_ENV_KEYS, HIDDEN_GATEWAY_ENV, GatewayConfigError
from booley.config.settings import BackendConfigError, get_agent_settings, load_agent_settings
from booley.core.booley_dir import resolve_main_checkout_root
from booley.harness import chat_checkout, chat_exit, codex_chat, worktree_cmd
from booley.harness.chat_checkout import Placement
from booley.harness.chat_exit import CreatedWorktree
from booley.runtime.booley_dir import resolve_checkout_booley_dir, resolve_common_booley_dir
from booley.runtime.foreground_child import exec_agent, run_foreground_child
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


# How long the launch waits for the Session writer lock while reserving its handle.
_HANDLE_WAIT_S = 10.0


def _claude_command(
    project_root: Path, agent_args: list[str], *, strict: bool
) -> tuple[str, list[str], str | None]:
    """Use the launch process's persistent handle when naming is available.

    The handle stays argv[0]; Booley adds `-n <handle>` unless the user named the session.
    Without a handle the launch falls back to plain `claude`, or raises when *strict*.
    """
    try:
        identity = capture_process_identity(os.getpid())
        if identity is None:
            raise RuntimeError("launch process identity is unavailable")
        handle = reserve_session_handle(
            resolve_common_booley_dir(project_root),
            process_session_key(identity),
            wait_s=_HANDLE_WAIT_S,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        if strict:
            raise
        print(f"NOTE: Claude tab handle is unavailable: {exc}", file=sys.stderr)
        return "claude", ["claude"], None
    argv = [handle] if _names_session(agent_args) else [handle, "-n", handle]
    return "claude", argv, handle


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


def _hide_gateway_from_claude() -> None:
    """Drop the gateway settings from this process before it becomes Claude.

    The gateway routes Codex only. Applies in an issued Sandbox with Codex on the
    gateway; a hand-written spec keeps its environment. The marker tells Doctor the
    settings were hidden, not missing.
    """
    if not issued_codex_gateway():
        return
    for key in GATEWAY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ[HIDDEN_GATEWAY_ENV] = "1"


class _NoHandleError(Exception):
    """A worktree launch could not reserve the session handle that names the worktree."""


class _SetupInterruptedError(Exception):
    """SIGINT, SIGHUP or SIGTERM arrived while the worktree was being set up."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = signum


@contextlib.contextmanager
def _signals_interrupt_setup() -> Iterator[None]:
    """Turn SIGINT/SIGHUP/SIGTERM into an exception so worktree setup rolls back, then restore.

    The first signal ignores all three until the context exits: a second Ctrl-C or the
    SIGHUP a closing terminal sends twice must not abort the rollback it triggered.
    """
    signals = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)

    def interrupt(signum: int, _frame: object) -> None:
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise _SetupInterruptedError(signum)

    previous = {number: signal.signal(number, interrupt) for number in signals}
    # The launch window holds SIGINT blocked until the agent's exec; setup must see it now,
    # or a Ctrl-C would wait out the whole creation and then kill the launch with the
    # worktree already made. The held state returns when setup ends.
    held_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, signals)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, held_mask)


def _agent_command(
    provider: str, project_root: Path, agent_args: list[str], *, strict: bool
) -> tuple[str, list[str], str | None]:
    """Return the program, argv and reserved handle (``None`` when unnamed) for *provider*.

    With *strict* a handle that cannot be reserved raises `_NoHandleError` instead of
    falling back to an unnamed tab.
    """
    try:
        if provider == "codex":
            return codex_chat.command(project_root, wait_s=_HANDLE_WAIT_S, strict=strict)
        if provider == "claude":
            return _claude_command(project_root, agent_args, strict=strict)
    except (OSError, ValueError, RuntimeError) as exc:
        raise _NoHandleError(str(exc)) from exc
    return provider, [provider], None


def _interrupted_notice(project_root: Path, name: str) -> str:
    """Say what an interrupted setup left: nothing, or the removal steps for what survived.

    A signal that lands while the creation script runs kills it before it can undo its
    work, so the claim that nothing remains is checked, not assumed.
    """
    worktree = worktree_cmd.worktree_path(project_root, name)
    branch = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/booley/{name}",
        ],
        check=False,
    )
    if not worktree.exists() and branch.returncode != 0:
        return f"Worktree setup interrupted; {name} was rolled back."
    return (
        f"Worktree setup interrupted; {name} may be left behind.\n"
        f"{worktree_cmd.worktree_removal_instructions(project_root, worktree, name)}"
    )


def _enter_worktree(
    project_root: Path, handle: str, reason: str | None, created: CreatedWorktree | None
) -> int | None:
    """Create the worktree named after *handle*, tell the parent, announce it and move there.

    Returns an exit code when the agent must not start, ``None`` after the move.
    """
    name = handle.lower()
    try:
        with _signals_interrupt_setup():
            worktree = worktree_cmd.create_worktree(project_root, name)
    except (KeyboardInterrupt, _SetupInterruptedError) as exc:
        signum = exc.signum if isinstance(exc, _SetupInterruptedError) else signal.SIGINT
        print(_interrupted_notice(project_root, name), file=sys.stderr)
        return 128 + int(signum)
    if worktree is None:
        return 1
    if created is not None:
        created.record(name)  # the parent's exit step needs the name once the agent ends
    print(
        f"Booley session {handle} works in worktree {worktree} on branch booley/{name} ({reason}).",
        file=sys.stderr,
    )
    os.chdir(worktree)
    return None


def _launch(
    provider: str,
    project_root: Path,
    agent_args: list[str],
    placement: Placement,
    created: CreatedWorktree | None = None,
) -> int:
    """Exec the agent CLI; runs in the forked child, so identities are the agent's own.

    A worktree placement first reserves the handle, creates the worktree it names, reports
    it through *created* and starts the agent there. Returns an exit code only when the
    launch fails: on success the process is replaced.
    """
    try:
        executable, argv, handle = _agent_command(
            provider, project_root, agent_args, strict=placement.worktree
        )
        if provider == "claude":
            _hide_gateway_from_claude()
        if placement.worktree:
            if handle is None:
                raise _NoHandleError(f"{provider} cannot carry a handle")
            if (
                code := _enter_worktree(project_root, handle, placement.reason, created)
            ) is not None:
                return code
        # Last statement before the process is replaced: the CLI exists and the
        # launch arguments are settled, so the notice only precedes a real start.
        print(f"Booley is starting the {provider} interactive agent.", file=sys.stderr)
        exec_agent(executable, [*argv, *agent_args])
    except _NoHandleError as exc:
        print(
            f"ERROR: cannot name a worktree for this session: {exc}. "
            "Run booley chat --here to use the main checkout.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        return _cli_not_found(provider)
    except OSError as exc:
        print(f"ERROR: could not launch agent CLI '{provider}': {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        # Ctrl-C while waiting for the Session writer lock: one line, no traceback.
        print("Launch interrupted.", file=sys.stderr)
        return 128 + int(signal.SIGINT)
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

    try:
        mode, flagged = _worktree_mode(args, booley_dir)
    except ValueError as exc:
        print(f"ERROR: booley.toml {exc}", file=sys.stderr)
        return 2

    # Existence only (F_OK): a non-executable file keeps the launch error below.
    if shutil.which(provider, mode=os.F_OK) is None:
        return _cli_not_found(provider)

    # A worktree that sat idle is removed before this launch allocates a handle and a name.
    chat_exit.collect_at_launch(project_root)
    # The child, not Booley, reserves the handle: the handle belongs to the agent's process.
    launch = functools.partial(_launch, provider, project_root, args.agent_args)
    return _run_in_checkout(launch, mode, flagged, project_root)


def _worktree_mode(args: argparse.Namespace, booley_dir: Path) -> tuple[str, bool]:
    """Return the placement mode and whether a flag chose it; the flag beats `[chat] worktree`."""
    # Bare `booley` builds its namespace without chat options, so the flag may be absent.
    flag = getattr(args, "chat_worktree", None)
    return (chat_worktree(booley_dir), False) if flag is None else (flag, True)


def _run_in_checkout(
    launch: Callable[[Placement, CreatedWorktree | None], int],
    mode: str,
    flagged: bool,
    project_root: Path,
) -> int:
    """Pick the checkout from where the launch happens, hold the main lock, run the child.

    Only a launch from the main checkout is placed: anywhere else (a linked worktree,
    outside Git) the agent starts in place, as before, without the lock or an exit prompt
    (Booley did not create that worktree). A worktree placement also gets the exit step.
    """
    main_root = resolve_main_checkout_root(project_root)
    if chat_checkout.launch_checkout(Path.cwd(), main_root) != "main":
        if mode == "always" and flagged:
            print(
                f"ERROR: booley chat --new-worktree runs only from the main checkout {main_root}.",
                file=sys.stderr,
            )
            return 2
        chat_exit.stamp_in_place_worktree(project_root)
        return run_foreground_child(functools.partial(launch, Placement(worktree=False), None))
    common = resolve_common_booley_dir(project_root)
    with chat_checkout.main_checkout_lock(common) as lock:
        session = (
            chat_checkout.main_checkout_session(common, main_root)
            if mode == "auto" and lock.held
            else None
        )
        placement = chat_checkout.choose_placement(
            mode, lock_held=lock.held, session=session, flagged=flagged
        )
        if mode == "auto" and placement.worktree:
            lock.release()  # not in main, so later launches may take it
        # Without `os.fork` (Windows) the agent replaces Booley: no session ever ends in it.
        if not placement.worktree or not hasattr(os, "fork"):
            return run_foreground_child(functools.partial(launch, placement, None))
        created = CreatedWorktree()
        try:
            return run_foreground_child(
                functools.partial(launch, placement, created),
                after_exit=functools.partial(
                    chat_exit.exit_step, project_root, created, lock.release
                ),
            )
        finally:
            created.close()
