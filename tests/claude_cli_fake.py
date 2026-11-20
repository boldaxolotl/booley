"""A stand-in Claude CLI for driving the real SDK in tests (POSIX only)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path

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
