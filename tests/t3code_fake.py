"""A fake T3 Code: the `t3` process boundary and the loopback port it serves."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from booley.runtime import incontainer_setup, mcp_config

TOKEN = "pairing-token-0123"
ADDED = (0, "Added project 1 (p) at /p.")


@dataclass
class T3CodeFake:
    """Record `t3 serve` and `t3 project add`; print the pairing token on stdout as T3 Code does."""

    running: bool = False
    add_replies: list[tuple[int, str] | OSError] = field(default_factory=list)
    spawn_error: OSError | None = None
    served: list[list[str]] = field(default_factory=list)
    added: list[list[str]] = field(default_factory=list)
    sinks: list[str] = field(default_factory=list)

    def serving(self, port: int, **_kwargs: object) -> bool:
        return port == mcp_config.http_port() or self.running

    def popen(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if self.spawn_error:
            raise self.spawn_error
        if cmd[0] == "t3":
            self.served.append(cmd)
            self.running = True
        self.sinks += [s.name for s in (kwargs["stdout"], kwargs["stderr"]) if hasattr(s, "name")]
        if hasattr(kwargs["stdout"], "write"):
            kwargs["stdout"].write(f"Token: {TOKEN}\n".encode())
        return SimpleNamespace(poll=lambda: None)

    def run(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.added.append(cmd)
        reply = self.add_replies.pop(0) if self.add_replies else ADDED
        if isinstance(reply, OSError):
            raise reply
        code, out = reply
        return subprocess.CompletedProcess(cmd, code, out, "")


def serve_t3_code(
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: bool = False,
    add_replies: list[tuple[int, str] | OSError] | None = None,
) -> T3CodeFake:
    """Route `incontainer_setup`'s port probe and `subprocess` use to a `T3CodeFake`."""
    fake = T3CodeFake(running=running, add_replies=list(add_replies or []))
    monkeypatch.setattr(incontainer_setup, "_port_is_serving", fake.serving)
    process = SimpleNamespace(
        Popen=fake.popen,
        run=fake.run,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(incontainer_setup, "subprocess", process)
    return fake
