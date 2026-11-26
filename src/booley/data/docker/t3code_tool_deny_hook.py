"""Codex ``PreToolUse`` hook: block the T3 Code tools the Sandbox turns off (#5511).

The Sandbox Image installs this file, with ``t3code-denied-tools.txt`` beside it, in
``/usr/local/libexec/booley`` and Codex's managed requirements run it for T3 Code tool calls.
It is separate from ``session_activity_hook.py``, which must never block a turn.

Codex blocks a call only on exit 2 with a reason on stderr; any other failure lets the call
proceed. A call this hook cannot check is therefore blocked the same way.

Standard library only: the client waits for it before every T3 Code tool call.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DENIED_TOOLS = Path(__file__).with_name("t3code-denied-tools.txt")
# Codex shows hooks the servers ``t3-code`` and ``t3-code-direct`` with ``-`` turned into
# ``_``. It appends ``_<12 hex>`` to a server name when a second server's name turns into the
# same, and to a tool name when another server's tool spells the same server and tool joined.
T3CODE_TOOL = re.compile(
    r"mcp__t3_code(?:_direct)?(?:_[0-9a-f]{12})?__(?P<tool>.+?)(?:_[0-9a-f]{12})?"
)


def denial(tool_name: str, denied: frozenset[str]) -> str | None:
    """Return why *tool_name* is blocked, or ``None`` when it may run."""
    match = T3CODE_TOOL.fullmatch(tool_name)
    if match is None or match["tool"] not in denied:
        return None
    return f"The Booley Sandbox turns off the T3 Code tool {match['tool']}."


def main() -> int:
    """Hook entry point: read the event from stdin; exit 2 blocks the call."""
    try:
        tool_name = json.loads(sys.stdin.buffer.read())["tool_name"]
        denied = frozenset(DENIED_TOOLS.read_text(encoding="utf-8").split())
        reason = denial(tool_name, denied)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        reason = f"The Booley Sandbox could not check this T3 Code tool call: {exc}"
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
