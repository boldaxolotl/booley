"""Validate the Sandbox Image's session-activity hooks for the Dashboard (ADR 0068)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from booley.harness.web_isolation import CLAUDE_POLICY, CODEX_POLICY
from booley.runtime.session_activity import END_EVENT, EVENT_STATES

HOOK_SCRIPT = Path("usr/local/libexec/booley/session_activity_hook.py")
# -I -S keeps user-site, PYTHON* overrides and site imports out of a per-event process.
HOOK_COMMAND = f"python3 -I -S /{HOOK_SCRIPT.as_posix()}"
# Events only one client emits. Codex has no failed-tool event (its next event
# after a failed call is PostToolUse or Stop) and no notifications; Claude Code
# has no interrupt event.
CLAUDE_ONLY = frozenset({"PostToolUseFailure", "Notification"})
CODEX_ONLY = frozenset({"Interrupt"})
CLAUDE_EVENTS = frozenset({*EVENT_STATES, END_EVENT}) - CODEX_ONLY
CODEX_EVENTS = frozenset({*EVENT_STATES, END_EVENT}) - CLAUDE_ONLY


def _hooked_events(hooks: Any, command: str) -> set[str]:
    """Events whose configuration runs *command*; malformed entries count as absent."""
    if not isinstance(hooks, dict):
        return set()
    found = set()
    for event, groups in hooks.items():
        handlers = [
            handler
            for group in (groups if isinstance(groups, list) else [])
            if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for handler in group["hooks"]
        ]
        if any(isinstance(item, dict) and item.get("command") == command for item in handlers):
            found.add(event)
    return found


def hooks_error(root: Path = Path("/")) -> str | None:
    """Return the first missing piece of the activity hooks, else ``None``."""
    if not (root / HOOK_SCRIPT).is_file():
        return f"activity hook script missing at {root / HOOK_SCRIPT}"
    sources = (
        ("Claude Code", root / CLAUDE_POLICY, json.loads, "claude", CLAUDE_EVENTS),
        ("Codex", root / CODEX_POLICY, tomllib.loads, "codex", CODEX_EVENTS),
    )
    for name, path, parse, client, required in sources:
        try:
            config = parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"{name} activity hooks unreadable at {path}: {exc}"
        hooks = config.get("hooks") if isinstance(config, dict) else None
        missing = sorted(required - _hooked_events(hooks, f"{HOOK_COMMAND} {client}"))
        if missing:
            return f"{name} activity hooks at {path} do not cover: {', '.join(missing)}"
    return None
