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


def record_sdk_options(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the Claude Agent SDK's ``query`` and return the options each call receives.

    The SDK is where Booley hands off to the Claude CLI; the fake answers with
    one successful result.
    """
    from claude_agent_sdk import ResultMessage

    from booley.runtime import _claude_backend

    seen: list[Any] = []

    async def query(*, prompt: Any, options: Any):
        seen.append(options)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="test-session",
            result="",
            total_cost_usd=0.01,
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr(_claude_backend, "query", query)
    return seen
