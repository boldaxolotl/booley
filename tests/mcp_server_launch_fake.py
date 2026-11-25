"""A fake of the command that starts a Sandbox's HTTP MCP server (``python -m booley.mcp.server``).

``McpServerLaunchFake.installed`` reserves a loopback port (bound but not listening, so
a probe is refused), points ``BOOLEY_MCP_HTTP_PORT`` at it, and replaces
``subprocess.Popen`` for that one command. A launch whose ``outcome`` is ``serves``
starts listening on the port, as the real server does once it is ready; ``exits``
returns a process that already exited with 3; ``unlaunchable`` raises ``OSError``.
Every other ``Popen`` call runs for real. ``launches`` records each launch's argv and
keyword arguments.
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.harness.process_fake import FakeProcess


class McpServerLaunchFake:
    def __init__(self, port: socket.socket) -> None:
        self.outcome = "serves"
        self.launches: list[tuple[list[str], dict[str, Any]]] = []
        self.listening = False
        self.port = port

    def serve(self) -> None:
        """Accept connections on the port, like a running server."""
        self.port.listen()
        self.listening = True

    def popen(self, real: Any, args: Any, **kwargs: Any) -> Any:
        if list(args[1:3]) != ["-m", "booley.mcp.server"]:
            return real(args, **kwargs)
        self.launches.append((list(args), kwargs))
        if self.outcome == "unlaunchable":
            raise OSError("no exec")
        if self.outcome == "exits":
            return FakeProcess(returncode=3)
        self.serve()
        return FakeProcess()

    @classmethod
    @contextmanager
    def installed(cls, monkeypatch: pytest.MonkeyPatch) -> Iterator[McpServerLaunchFake]:
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", str(port.getsockname()[1]))
            fake = cls(port)
            real = subprocess.Popen
            monkeypatch.setattr(
                subprocess, "Popen", lambda args, **kwargs: fake.popen(real, args, **kwargs)
            )
            yield fake
