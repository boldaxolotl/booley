"""Bind a loopback port before a server announces it, so a bind failure is a Booley error."""

from __future__ import annotations

import contextlib
import os
import socket


class PortUnavailableError(Exception):
    """The port could not be bound; the message names the address and the reason."""


def listen(host: str, port: int) -> socket.socket:
    """Return a socket listening on ``host:port``, or raise :class:`PortUnavailableError`.

    Listening before the caller announces the address means a client that connects right
    after the announcement waits in the backlog instead of being refused.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            # What asyncio and uvicorn set on POSIX; on Windows it would let a second
            # server share a busy port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen()
    except OSError as exc:
        sock.close()
        raise PortUnavailableError(
            f"cannot listen on {host}:{port}: {exc.strerror or exc}"
        ) from exc
    return sock


def serve(app: object, sock: socket.socket) -> None:
    """Serve ``app`` on the listening ``sock`` until interrupted, then close it."""
    import uvicorn

    # uvicorn types ``app`` as an ASGI callable; callers pass Starlette apps.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))  # type: ignore[arg-type]
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.run(sockets=[sock])
    finally:
        sock.close()
