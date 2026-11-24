"""A stand-in Claude CLI for driving the real SDK in tests (POSIX only)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import anyio
import pytest

_SCRIPT = """#!{python}
import json, sys
if "--version" in sys.argv:
    print("2.0.0")
    sys.exit(0)
events = json.loads({events!r})
sent = False
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        reply = {{"subtype": "success", "request_id": message["request_id"], "response": {{}}}}
        print(json.dumps({{"type": "control_response", "response": reply}}), flush=True)
    elif message.get("type") == "user" and not sent:
        sent = True
        for event in events:
            print(json.dumps(event), flush=True)
"""


def write_claude_cli(directory: Path, events: list[dict]) -> Path:
    """Write an executable CLI that sends ``events`` after the first user message."""
    path = directory / "claude-fake"
    text = _SCRIPT.format(python=sys.executable, events=json.dumps(events))
    path.write_bytes(textwrap.dedent(text).encode("utf-8"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def sdk_message(kind: str, **fields: Any) -> Any:
    """Build a Claude Agent SDK message of class ``kind`` without its constructor.

    The constructors differ between SDK releases (and the harness stubs the SDK
    when it is not installed), so each field is set on a bare instance.
    """
    import claude_agent_sdk

    message = object.__new__(getattr(claude_agent_sdk, kind))
    if kind == "AssistantMessage":
        fields = {"content": [], "model": "test-model", "usage": None, **fields}
    for name, value in fields.items():
        object.__setattr__(message, name, value)
    return message


def sdk_result(text: str = "done", **fields: Any) -> Any:
    """A successful ``ResultMessage`` with every field Booley reads."""
    defaults: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "result": text,
        "structured_output": None,
        "total_cost_usd": 0.01,
        "usage": None,
    }
    return sdk_message("ResultMessage", **{**defaults, **fields})


def replay_sdk_stream(monkeypatch: pytest.MonkeyPatch, *steps: Any) -> list[Any]:
    """Replace the Claude Agent SDK's ``query`` with a stream that replays ``steps``.

    The SDK is where Booley hands off to the Claude CLI. Every call walks the
    steps in order: a message is yielded, an exception is raised, a callable is
    called with the call's options (and may raise), a number is awaited as a
    sleep. Returns the options of each call, so its length is the attempt count.
    """
    from booley.runtime import _claude_backend

    seen: list[Any] = []

    async def query(*, prompt: Any, options: Any):
        seen.append(options)
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step(options)
            elif isinstance(step, (int, float)):
                await anyio.sleep(step)
            else:
                yield step

    monkeypatch.setattr(_claude_backend, "query", query)
    return seen


def record_sdk_options(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replay one successful result and return the options each call receives."""
    return replay_sdk_stream(
        monkeypatch, sdk_result("", usage={"input_tokens": 1, "output_tokens": 1})
    )
