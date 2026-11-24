"""A stand-in Claude CLI for driving the real SDK in tests (POSIX only)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

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


def sdk_result(text: str = "done", **fields: Any) -> Any:
    """Build a successful ``ResultMessage`` of the loaded Claude Agent SDK.

    Every field the backend reads is set, so the stub SDK a harness-only run
    loads and the real SDK take the same message.
    """
    from claude_agent_sdk import ResultMessage

    values: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "result": text,
        "structured_output": None,
        "total_cost_usd": None,
        "usage": None,
    }
    values.update(fields)
    return ResultMessage(**values)


def sdk_message(kind: str, **fields: Any) -> Any:
    """Build an ``AssistantMessage`` or ``UserMessage`` of the loaded SDK with ``fields`` set.

    The harness conftest installs a stub SDK whose message classes take no
    arguments when the real one was not imported first, so the fields are set
    directly. Content blocks are plain namespaces: the backend reads them only
    through ``getattr``.
    """
    import claude_agent_sdk

    message = object.__new__(getattr(claude_agent_sdk, kind))
    message.__dict__.update(fields)
    return message


def replay_sdk_stream(monkeypatch: pytest.MonkeyPatch, *steps: Any) -> list[Any]:
    """Replace the Claude Agent SDK's ``query`` with a scripted stream; return each call's options.

    The SDK is where Booley hands off to the Claude CLI. Each step is one of:
    an SDK message (yielded), a number (seconds to wait), an exception
    (raised), or a callable (called with the call's options, for example to
    write CLI stderr through ``options.stderr``).
    """
    import anyio

    from booley.runtime import _claude_backend

    seen: list[Any] = []

    async def query(*, prompt: Any, options: Any):
        seen.append(options)
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, int | float):
                await anyio.sleep(step)
            elif callable(step):
                step(options)
            else:
                yield step

    monkeypatch.setattr(_claude_backend, "query", query)
    return seen


def record_sdk_options(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the Claude Agent SDK's ``query`` and return the options each call receives.

    The fake answers with one successful result.
    """
    return replay_sdk_stream(
        monkeypatch,
        sdk_result("", total_cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 1}),
    )
