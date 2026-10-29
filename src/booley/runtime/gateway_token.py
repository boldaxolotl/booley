"""Gateway bearer token: the Codex ``auth.command`` helper and the token command.

Codex runs ``python -P -m booley.runtime.gateway_token`` and reads the token
from stdout. The helper prints ``BOOLEY_GATEWAY_TOKEN`` from the environment
Codex inherited, so it never touches disk.

``BOOLEY_GATEWAY_TOKEN_COMMAND`` is the second token source (#2267, ADR 0080
amendment). The Booley process that spawns Codex runs the command, keeps its
output in memory for :data:`TOKEN_TTL_S`, and hands the result to the Codex
child as ``BOOLEY_GATEWAY_TOKEN``. The token is never logged, put in an error
message, or written to disk; a failing command's *stderr* is shown to the user
verbatim, so the command must never print the token there.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from booley.config.gateway import (
    GATEWAY_TOKEN_COMMAND_ENV,
    GatewayConfigError,
    GatewaySettings,
    gateway_settings_from_env,
)
from booley.runtime.process_group import (
    capture_process_group,
    force_group_termination,
    new_group_kwargs,
)

logger = logging.getLogger(__name__)

TOKEN_TTL_S = 300
COMMAND_TIMEOUT_S = 30
# Bound on collecting stderr after the timeout kill, in case a descendant left the group.
_REAP_TIMEOUT_S = 5


class GatewayTokenCommandError(GatewayConfigError):
    """The token command failed; the message carries its stderr, never its stdout."""


def _failure(reason: str, stderr: bytes | None = None) -> GatewayTokenCommandError:
    """Build the error for *reason*, appending the command's stderr verbatim."""
    message = f"{GATEWAY_TOKEN_COMMAND_ENV} {reason}"
    detail = (stderr or b"").decode("utf-8", errors="replace").rstrip()
    if detail:
        message = f"{message}\n{detail}"
    return GatewayTokenCommandError(message)


def _stderr_after_kill(proc: subprocess.Popen[bytes]) -> bytes:
    """Collect what the killed command wrote to stderr (empty if the pipes stay open)."""
    try:
        return proc.communicate(timeout=_REAP_TIMEOUT_S)[1]
    except subprocess.TimeoutExpired:
        # A descendant escaped the group and holds the pipes: release ours.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        return b""


def _validated_token(stdout: bytes, stderr: bytes) -> str:
    """Return the stripped token, or raise without echoing *stdout*."""
    token = stdout.decode("utf-8", errors="replace").strip()
    if not token:
        raise _failure("printed no token", stderr)
    if not token.isascii() or any(not c.isprintable() or c.isspace() for c in token):
        raise _failure("printed an invalid token (it must be one printable ASCII line)", stderr)
    return token


def run_token_command(command: str, *, timeout_s: int = COMMAND_TIMEOUT_S) -> str:
    """Run *command* (split like a shell word list, no shell) and return its token.

    The command inherits this process's environment. Every failure raises
    :class:`GatewayTokenCommandError` with no argv and no stdout in the message.
    """
    try:
        proc = subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **new_group_kwargs(),
        )
    except (OSError, ValueError) as exc:
        # Raised below, outside the handler: the OSError names the program and the
        # chained context must not carry it.
        start_failure = _failure(f"could not be started ({type(exc).__name__})")
    else:
        start_failure = None
    if start_failure is not None:
        raise start_failure
    group = capture_process_group(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # TimeoutExpired holds the argv and partial stdout; keep it out of the chain.
        timed_out = True
    else:
        timed_out = False
    if timed_out:
        force_group_termination(proc, group)
        raise _failure(f"timed out after {timeout_s} s", _stderr_after_kill(proc))
    if proc.returncode != 0:
        raise _failure(f"exited {proc.returncode}", stderr)
    return _validated_token(stdout, stderr)


class GatewayTokenCache:
    """In-memory, time-limited cache of one token command's output.

    One lock is held across a run, so concurrent callers share a single command
    run. Failures are not cached. The token and command stay out of ``repr``.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, ttl_s: float = TOKEN_TTL_S
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entry: tuple[str, str, float] | None = None  # (command, token, expires_at)

    def __repr__(self) -> str:
        return f"GatewayTokenCache(ttl_s={self._ttl_s}, cached={self._entry is not None})"

    def get(self, command: str) -> str:
        """Return the cached token for *command*, running it when absent or expired."""
        with self._lock:
            entry = self._entry
            if entry is not None and entry[0] == command and self._clock() < entry[2]:
                logger.debug("%s: reusing the cached token", GATEWAY_TOKEN_COMMAND_ENV)
                return entry[1]
            token = run_token_command(command)
            self._entry = (command, token, self._clock() + self._ttl_s)
            logger.debug("%s: ran the command and cached its token", GATEWAY_TOKEN_COMMAND_ENV)
            return token

    def invalidate(self) -> None:
        """Forget the cached token so the next :meth:`get` re-runs the command."""
        with self._lock:
            self._entry = None
        logger.debug("%s: cached token invalidated", GATEWAY_TOKEN_COMMAND_ENV)


_CACHE = GatewayTokenCache()


def gateway_token_for(settings: GatewaySettings) -> str:
    """The bearer token for *settings*: the static value, or the cached command output."""
    if settings.token_command:
        return _CACHE.get(settings.token_command)
    assert settings.token, "gateway settings always carry exactly one token source"
    return settings.token


def refresh_gateway_token() -> None:
    """Drop the cached command output (the gateway rejected it)."""
    _CACHE.invalidate()


def main() -> int:
    """Write the token to stdout, or the config or command error to stderr."""
    try:
        token = gateway_token_for(gateway_settings_from_env())
    except GatewayConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
