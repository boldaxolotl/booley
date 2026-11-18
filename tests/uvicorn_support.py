"""One stand-in for uvicorn's serving loop, shared by tests of Booley's HTTP servers."""

from __future__ import annotations

import socket

import pytest
import uvicorn


def capture_serving(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, tuple[str, int]]]:
    """Replace ``uvicorn.Server.run`` with a recorder that returns at once.

    Each served call appends ``(app, (host, port))`` of the pre-bound socket it was
    handed, so a test never serves forever.
    """
    served: list[tuple[object, tuple[str, int]]] = []

    def run(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        if sockets is None:  # uvicorn.run binds itself: the stand-in never starts a server
            return
        served.append((self.config.app, sockets[0].getsockname()[:2]))

    monkeypatch.setattr(uvicorn.Server, "run", run)
    return served
