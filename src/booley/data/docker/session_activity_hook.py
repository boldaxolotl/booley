"""Agent-client lifecycle hook: record one session's activity for the Dashboard.

The Sandbox Image installs this file at
``/usr/local/libexec/booley/session_activity_hook.py`` and configures Claude
Code and Codex to run ``python3 -I -S <this file> <claude|codex>`` on lifecycle
events, with the event's JSON on stdin (ADR 0068).

The client waits for this process on every prompt and tool call, so it is a
standalone, standard-library-only script: importing the ``booley`` package
would cost about 0.1 s per event. It therefore repeats the few storage rules
of ``booley.runtime.session_activity``, the Session Registry key formats and
the Booley Directory resolution order of ``booley.core.booley_dir``;
``tests/harness/test_session_activity_hook.py`` holds the two in agreement.

It prints nothing to stdout (clients feed hook output back to the model),
reports a problem on stderr only, and always exits 0, so it can never block or
alter a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

CLIENTS = ("claude", "codex")
SCHEMA = 1
WORKING = "working"
WAITING_TOOL = "waiting for tool call"
WAITING_USER = "waiting for user input"
# Lifecycle events that carry an activity state. Every other event is ignored.
EVENT_STATES = {
    "UserPromptSubmit": WORKING,
    "PreToolUse": WAITING_TOOL,
    "PostToolUse": WORKING,
    "PostToolUseFailure": WORKING,
    "PermissionRequest": WAITING_USER,
    "Stop": WAITING_USER,
    # A turn the user interrupts ends without Stop. Codex reports the interrupt;
    # Claude Code reports that it is idle or asking through a notification.
    "Interrupt": WAITING_USER,
    "Notification": WAITING_USER,
}
END_EVENT = "SessionEnd"
# What SessionEnd leaves behind: the same signal file, marked as ended.
ENDED = "ended"
# The only Claude Code notifications that mean "waiting for the user".
WAITING_NOTIFICATIONS = frozenset({"idle_prompt", "permission_prompt", "elicitation_dialog"})
CLIENT_COMMAND = "claude"

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_ANCESTORS = 32
MAX_SIGNALS = 4096
MAX_TOOL_NAME = 128
MAX_THREAD_ID = 1024
MAX_PARENT_DIRS = 128
BOOLEY_DIR_NAME = ".booley"
CODEX_NATIVE_NAMES = frozenset(
    {
        "codex",
        "codex-x86_64-unknown-linux-musl",
        "codex-aarch64-unknown-linux-musl",
        "codex-x86_64-unknown-linux-gnu",
        "codex-aarch64-unknown-linux-gnu",
    }
)


def _process_key(proc_root: Path, pid: int) -> str | None:
    """Session Registry key of a process: PID namespace, PID and start ticks."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        scope = (proc_root / str(pid) / "ns" / "pid").readlink()
    except (OSError, IndexError, ValueError):
        return None
    return f"pid:{scope}:{pid}:{start_ticks}"


def _ancestors(proc_root: Path, pid: int) -> list[int]:
    """Parent chain of *pid*, nearest first, bounded and stopping before PID 1."""
    chain = []
    current = pid
    for _ in range(MAX_ANCESTORS):
        try:
            stat = (proc_root / str(current) / "stat").read_text(encoding="utf-8")
            current = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
        if current <= 1:
            break
        chain.append(current)
    return chain


def _signal_filename(key: str) -> str:
    """Opaque filename; logical keys never become filesystem components."""
    return hashlib.sha256(key.encode()).hexdigest() + ".json"


def _claude_key(env: Mapping[str, str], project: Path, proc_root: Path, pid: int) -> str | None:
    """Key of the Claude Code process this hook descends from.

    The registry keys a Claude Code session by the process that owns its MCP
    connections; a hook runs as that process's descendant. ``CLAUDE_PID`` names
    it directly; without it, the nearest ancestor that already has a registry
    row is the session. The walk stops at a nearer Claude Code process that has
    no row: that process owns this hook, and crediting its events to an outer
    session would be a guess. Anything else records nothing.
    """
    chain = _ancestors(proc_root, pid)
    claimed = env.get("CLAUDE_PID", "")
    if claimed.isdecimal() and int(claimed) in chain:
        return _process_key(proc_root, int(claimed))
    sessions = project / "runtime" / "sessions"
    for ancestor in chain:
        key = _process_key(proc_root, ancestor)
        if key is not None and (sessions / _signal_filename(key)).is_file():
            return key
        if _command_name(proc_root, ancestor) == CLIENT_COMMAND:
            return None
    return None


def _command_name(proc_root: Path, pid: int) -> str:
    """The kernel's short command name of *pid*, or "" when unreadable."""
    try:
        return (proc_root / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _codex_key(payload: Mapping[str, object]) -> str | None:
    """Key of the Codex thread: a subagent reports its own thread as ``agent_id``."""
    thread = payload.get("agent_id") or payload.get("session_id")
    if isinstance(thread, str) and 0 < len(thread) <= MAX_THREAD_ID and thread.isprintable():
        return "codex:" + thread
    return None


def _chat_aliases(project: Path) -> dict[str, str]:
    """Validate the session-map contract of runtime.handles.HandleStore, but stricter.

    Only the Booley Directory entry itself (*project*) may be a link, as in the host
    store: the Sandbox ``BOOLEY_DIR=/booley-project`` is an image symlink to
    ``/work/.booley``, and a host ``.booley`` may be a link. Links above or below
    that entry stay refused. This file runs as ``python3 -I -S`` without Booley
    imports, so the rule of ``booley.core.booley_dir_links`` is inlined.
    """
    path = project / "runtime/sessions/handles/map.json"
    if any(part.is_symlink() for part in (path, *path.parents) if part != project):
        raise ValueError("handle map is redirected")
    with path.open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("handle map exceeds size bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("unsupported handle map schema")
    aliases, entries = data.get("aliases"), data.get("entries")
    if not isinstance(aliases, dict) or not isinstance(entries, dict):
        raise ValueError("invalid handle map")
    for mapping in (entries, aliases):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise ValueError("handle mapping must contain strings")
    if type(data.get("next_number")) is not int:
        raise ValueError("invalid handle counter")
    return aliases


def _codex_process(proc_root: Path, pid: int) -> tuple[str, bool]:
    """Recognize CLI boundaries using the same rule as runtime.chat_sessions."""
    executable = Path(
        str((proc_root / str(pid) / "exe").readlink()).removesuffix(" (deleted)")
    ).name
    if executable not in CODEX_NATIVE_NAMES:
        return executable, False
    with (proc_root / str(pid) / "cmdline").open("rb") as stream:
        argv = stream.read(4096).split(b"\0")
    return executable, argv[1:2] != [b"app-server"]


def _codex_chat_key(project: Path, proc_root: Path, pid: int) -> str | None:
    """Only the launch's own CLI and its helpers can own the tab's activity."""
    nearest_client = child = None
    try:
        aliases = _chat_aliases(project)
        for ancestor in _ancestors(proc_root, pid):
            executable, client = _codex_process(proc_root, ancestor)
            if client and nearest_client is None:
                nearest_client = ancestor
            key = _process_key(proc_root, ancestor)
            if key is not None and aliases.get("chat:codex:" + key) == key:
                expected = child if executable in {"node", "nodejs"} else ancestor
                return key if nearest_client == expected else None
            child = ancestor
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass  # Handle storage must not disable ordinary thread activity.
    return None


def _write(root: Path, key: str, state: str, tool: str, now: float) -> None:
    """Publish the session's signal with one atomic replacement."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / _signal_filename(key)
    if path.is_symlink():
        raise OSError(f"session activity storage is redirected: {path}")
    if not path.exists():
        with os.scandir(root) as scan:
            if sum(1 for _, _entry in zip(range(MAX_SIGNALS), scan, strict=False)) >= MAX_SIGNALS:
                raise OSError("session activity store is full")
    signal = {
        "schema": SCHEMA,
        "key": key,
        "state": state,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    if tool:
        signal["tool"] = tool
    # One staging name per process; the dot prefix and suffix keep it out of every reader.
    # No fsync: a signal lost to a crash only shows as unknown until the next event.
    temporary = root / f".{path.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(signal, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _reports_activity(payload: Mapping[str, object]) -> bool:
    """Whether this event says anything about activity; most notifications do not."""
    event = payload.get("hook_event_name")
    if event == "Notification":
        return payload.get("notification_type") in WAITING_NOTIFICATIONS
    return event == END_EVENT or event in EVENT_STATES


def record(
    client: str,
    payload: Mapping[str, object],
    project: Path,
    now: float,
    env: Mapping[str, str] | None = None,
    proc_root: Path = Path("/proc"),
    pid: int | None = None,
) -> bool:
    """Store the activity one hook event reports. Returns whether the store changed."""
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or not _reports_activity(payload):
        return False
    if client == "codex":
        key = _codex_chat_key(project, proc_root, os.getpid() if pid is None else pid)
        if key is not None and payload.get("agent_id") not in (None, ""):
            return False  # A subagent's Stop must not mark the main session idle.
        key = key or _codex_key(payload)
    else:
        key = _claude_key(
            os.environ if env is None else env,
            project,
            proc_root,
            os.getpid() if pid is None else pid,
        )
    if key is None:
        return False
    root = project / "runtime" / "session_activity"
    # Refuse a store reached through a symlink below the Booley Directory.
    for part in (root, root.parent):
        if part.is_symlink():
            raise OSError(f"session activity storage is redirected: {part}")
    if event == END_EVENT:
        _write(root, key, ENDED, "", now)
        return True
    state = EVENT_STATES[event]
    tool = payload.get("tool_name")
    named = state == WAITING_TOOL and isinstance(tool, str) and tool.isprintable()
    _write(root, key, state, tool[:MAX_TOOL_NAME] if named else "", now)
    return True


def _toml_booley_dir(directory: Path) -> Path | None:
    """``[project].dir`` of the booley.toml in *directory*, when it names a directory."""
    import tomllib  # Deferred: only a checkout with a booley.toml and no env override pays.

    try:
        with (directory / "booley.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    configured = project.get("dir") if isinstance(project, dict) else None
    if not isinstance(configured, str) or not configured or "\x00" in configured:
        return None
    resolved = (directory / configured).resolve()
    return resolved if resolved.is_dir() else None


def resolve_booley_dir(env: Mapping[str, str], cwd: Path) -> Path | None:
    """The Booley Directory this session works in, or ``None`` outside a Project.

    Same order as Booley itself: ``BOOLEY_DIR`` (always set in a
    Sandbox), then the nearest booley.toml ``[project].dir``, then the nearest
    ``.booley`` directory.
    """
    configured = env.get("BOOLEY_DIR", "")
    if configured:
        return Path(configured) if Path(configured).is_dir() else None
    current = Path(cwd).resolve()
    parents = [current, *current.parents][:MAX_PARENT_DIRS]
    for directory in parents:
        if (directory / "booley.toml").is_file():
            override = _toml_booley_dir(directory)
            if override is not None:
                return override
            break
    for directory in parents:
        candidate = directory / BOOLEY_DIR_NAME
        if candidate.is_dir() and any(
            (candidate / name).is_file() for name in ("booley.toml", "pipeline.toml")
        ):
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    """Hook entry point. Never fails the client's turn: every problem exits 0."""
    args = sys.argv[1:] if argv is None else argv
    now = time.time()
    try:
        if len(args) != 1 or args[0] not in CLIENTS:
            raise ValueError(f"expected one client of {CLIENTS}, got {args}")
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("hook payload exceeds the size bound")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        project = resolve_booley_dir(os.environ, Path.cwd())
        if project is not None:
            record(args[0], payload, project, now)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        # RecursionError: a deeply nested payload within the size bound.
        # stderr at exit 0 is ignored by both clients except in their debug logs.
        print(f"booley session activity not recorded: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
