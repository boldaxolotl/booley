"""Agent-client lifecycle hook: record one session's activity and conversation.

The Sandbox Image installs this file at
``/usr/local/libexec/booley/session_activity_hook.py`` and configures Claude
Code and Codex to run ``python3 -I -S <this file> <claude|codex>`` on lifecycle
events, with the event's JSON on stdin (ADR 0068).

The client waits for this process on every prompt and tool call, so it is a
standalone, standard-library-only script: importing the ``booley`` package
would cost about 0.1 s per event. It therefore repeats the few storage rules
of ``booley.runtime.session_activity``, the Session Registry key formats and
the Main Booley Directory resolution of ``booley.core.booley_dir``;
``tests/harness/test_session_activity_hook.py`` holds the two in agreement.

Two responsibilities share this script because they share its installation,
its stdlib-only constraint, and its Main Booley Directory resolution:

* Activity (Dashboard): every event in ``EVENT_STATES`` and ``SessionEnd``
  writes one signal under ``runtime/session_activity``.
* Conversation records (ADR 0068, #2610): ``SessionStart`` in an interactive
  worktree (``<Main Booley Directory>/worktrees/<name>``) writes
  ``worktrees/.conversations/<sha256(client + ":" + conversation_id)>.json``::

      {
          "schema": 1,
          "client": "claude" | "codex",
          "conversation_id": ...,
          "cwd": ...,
          "worktree": "<name>",
          "handle": "S<N>" | null,
          "process_key": ... | null,
          "at": "<UTC RFC 3339>",
          "git_stamp": <st_mtime_ns of worktrees/<name>/.git>,
      }

  A later start of the same conversation replaces the record, or removes it
  when the conversation starts outside a worktree, so a record names the
  worktree last used. A record lives while ``worktrees/<worktree>`` exists. It
  names the worktree by name only: a reader treats a worktree whose ``.git`` file
  has an ``st_mtime_ns`` other than ``git_stamp`` as another worktree of the same name.

It prints nothing to stdout (clients feed hook output back to the model),
reports a problem on stderr only, and always exits 0, so it can never block or
alter a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from itertools import islice
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
# Not an activity event: it records which worktree a conversation started in.
START_EVENT = "SessionStart"
# What SessionEnd leaves behind: the same signal file, marked as ended.
ENDED = "ended"
# The only Claude Code notifications that mean "waiting for the user".
WAITING_NOTIFICATIONS = frozenset({"idle_prompt", "permission_prompt", "elicitation_dialog"})
# Claude Code's Stop lists in-flight background work as ``background_tasks``.
# These task types are finite work whose result resumes the turn, so the
# session waits for them, not for the user. Shells and monitors are left out:
# a dev server or log watcher may run for the whole session.
AWAITED_TASK_TYPES = frozenset({"MCP task", "subagent", "workflow"})
STOP_EVENT = "Stop"
NOTIFICATION_EVENT = "Notification"
IDLE_NOTIFICATION = "idle_prompt"
CLIENT_COMMAND = "claude"

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_ANCESTORS = 32
MAX_SIGNALS = 4096
MAX_CONVERSATIONS = 4096
MAX_CWD = 4096
CONVERSATIONS_DIR = ".conversations"
# One path component, as booley.runtime.worktree_usage.WORKTREE_NAME.
WORKTREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_TOOL_NAME = 128
MAX_THREAD_ID = 1024
# json escapes a character to at most 6 bytes: bound for a cwd, a thread id and the fixed fields.
MAX_RECORD_BYTES = 6 * (MAX_CWD + MAX_THREAD_ID) + 1024
MAX_PARENT_DIRS = 128
MAX_HANDLE_MAP_BYTES = 16 * 1024 * 1024
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
    thread = _valid_id(payload.get("agent_id") or payload.get("session_id"))
    return None if thread is None else "codex:" + thread


def _valid_id(value: object) -> str | None:
    """*value* when it is usable as a thread or conversation identifier, else ``None``."""
    ok = isinstance(value, str) and 0 < len(value) <= MAX_THREAD_ID and value.isprintable()
    return value if ok and isinstance(value, str) else None


def _handle_map(project: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the session-map contract of runtime.handles.HandleStore, but stricter.

    A ``chat:codex:`` marker exists only where the host store accepted the map when
    the tab was launched, so this read needs only a bounded parse. Oversized or
    misshapen documents yield no aliases (thread attribution); unreadable or
    non-JSON files raise to the caller, which does the same.
    """
    path = project / "runtime/sessions/handles/map.json"
    if any(part.is_symlink() for part in (path, *path.parents) if part != project):
        raise ValueError("handle map is redirected")
    with path.open("rb") as stream:
        raw = stream.read(MAX_HANDLE_MAP_BYTES + 1)
    if len(raw) > MAX_HANDLE_MAP_BYTES:
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
    return entries, aliases


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
        aliases = _handle_map(project)[1]
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


def _background_wait(payload: Mapping[str, object]) -> str:
    """Label of the awaited background work a Stop leaves in flight, or "".

    The first awaited task names the wait: by its tool when that is usable
    text, otherwise by its type. Further awaited tasks only add a count.
    """
    tasks = payload.get("background_tasks")
    awaited = [
        task
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict) and task.get("type") in AWAITED_TASK_TYPES
    ]
    if not awaited:
        return ""
    label = awaited[0].get("tool")
    if not isinstance(label, str) or not label or not label.isprintable():
        label = awaited[0]["type"]
    more = f" +{len(awaited) - 1}" if len(awaited) > 1 else ""
    return label[: MAX_TOOL_NAME - len(more)] + more


def _waits_in_background(root: Path, key: str) -> bool:
    """Whether the session's current signal is a Stop that left background work."""
    path = root / _signal_filename(key)
    try:
        if path.is_symlink():
            return False
        with path.open("rb") as stream:
            data = json.loads(stream.read(4096))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(data, dict) and data.get("background") is True


def _write(
    root: Path, key: str, state: str, tool: str, now: float, *, background: bool = False
) -> None:
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
    if background:
        signal["background"] = True
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
    """Store the activity one hook event reports, or on SessionStart the conversation record.

    Returns whether a record was written.
    """
    event = payload.get("hook_event_name")
    if event == START_EVENT:
        return _record_conversation(
            client,
            payload,
            project,
            now,
            os.environ if env is None else env,
            proc_root,
            os.getpid() if pid is None else pid,
        )
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
    return _record_state(root, key, event, payload, now)


def _record_state(
    root: Path, key: str, event: str, payload: Mapping[str, object], now: float
) -> bool:
    """Store the activity state one event means. Returns whether the store changed."""
    waiting_on = _background_wait(payload) if event == STOP_EVENT else ""
    if waiting_on:
        _write(root, key, WAITING_TOOL, waiting_on, now, background=True)
        return True
    idle = event == NOTIFICATION_EVENT and payload.get("notification_type") == IDLE_NOTIFICATION
    if idle and _waits_in_background(root, key):
        return False  # The turn ended earlier with background work still running.
    state = EVENT_STATES[event]
    tool = payload.get("tool_name")
    label = (
        tool[:MAX_TOOL_NAME]
        if state == WAITING_TOOL and isinstance(tool, str) and tool.isprintable()
        else ""
    )
    _write(root, key, state, label, now)
    return True


def _worktree_name(path: Path, project: Path) -> str | None:
    """Name of the interactive worktree containing *path*, else ``None``.

    Mirrors ``booley.runtime.worktree_usage.interactive_worktree_name``: the first
    component below ``<project>/worktrees`` names a linked worktree (its ``.git``
    is a file). Both sides are resolved, so the Sandbox spelling of the Main
    Booley Directory compares equal to the host one.
    """
    try:
        relative = path.resolve().relative_to((project / "worktrees").resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not WORKTREE_NAME.fullmatch(name) or not (project / "worktrees" / name / ".git").is_file():
        return None
    return name


def _claude_process_key(env: Mapping[str, str], proc_root: Path, pid: int) -> str | None:
    """Key of the Claude Code process this hook descends from, at session start.

    No registry row exists yet, so ``_claude_key`` cannot find the session:
    ``CLAUDE_PID`` names the process directly, else the nearest ancestor whose
    command is ``claude`` is it.
    """
    chain = _ancestors(proc_root, pid)
    claimed = env.get("CLAUDE_PID", "")
    if claimed.isdecimal() and int(claimed) in chain:
        return _process_key(proc_root, int(claimed))
    owner = next((a for a in chain if _command_name(proc_root, a) == CLIENT_COMMAND), None)
    return None if owner is None else _process_key(proc_root, owner)


def _handle_label(project: Path, names: list[str]) -> str | None:
    """Handle label of the first of *names* the Session handle map allocates."""
    try:
        entries, aliases = _handle_map(project)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None  # Handle storage must not disable the record.
    for name in names:
        label = entries.get(name if name in entries else aliases.get(name, ""))
        if label is not None:
            return label
    return None


def _conversations_root(project: Path) -> Path:
    """The record directory, refused when it or ``worktrees`` is a link."""
    root = project / "worktrees" / CONVERSATIONS_DIR
    for part in (root, root.parent):
        if part.is_symlink():
            raise OSError(f"conversation storage is redirected: {part}")
    return root


def _record_gone(path: Path, project: Path) -> bool:
    """Whether *path* is a record whose worktree directory no longer exists."""
    if path.name.startswith(".") or path.is_symlink():
        return False
    try:
        with path.open("rb") as stream:
            record = json.loads(stream.read(MAX_RECORD_BYTES))
    except (OSError, ValueError, UnicodeError):
        return False
    worktree = record.get("worktree") if isinstance(record, dict) else None
    if not isinstance(worktree, str) or not WORKTREE_NAME.fullmatch(worktree):
        return False
    return not os.path.lexists(project / "worktrees" / worktree)


def _make_room(root: Path, project: Path) -> None:
    """Ensure one more record fits; at the bound, evict records of gone worktrees first."""
    with os.scandir(root) as scan:
        visible = (entry for entry in scan if not entry.name.startswith("."))
        paths = [Path(entry.path) for entry in islice(visible, MAX_CONVERSATIONS)]
    if len(paths) < MAX_CONVERSATIONS:
        return
    gone = [path for path in paths if _record_gone(path, project)]
    for path in gone:
        path.unlink(missing_ok=True)
    if not gone:
        raise OSError("conversation store is full")


def _conversation_record(
    client: str,
    conversation: str,
    cwd: str,
    worktree: str,
    now: float,
    project: Path,
    owner: tuple[Mapping[str, str], Path, int],
) -> dict[str, object]:
    """The record of a conversation starting in *worktree*; *owner* locates its process."""
    env, proc_root, pid = owner
    if client == "codex":
        key = _codex_chat_key(project, proc_root, pid)
        names = ([key] if key else []) + ["codex:" + conversation]
    else:
        key = _claude_process_key(env, proc_root, pid)
        names = [key] if key else []
    return {
        "schema": SCHEMA,
        "client": client,
        "conversation_id": conversation,
        "cwd": cwd,
        "worktree": worktree,
        "handle": _handle_label(project, names),
        "process_key": key,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        # Compared for equality by a reader: identifies this worktree incarnation.
        "git_stamp": (project / "worktrees" / worktree / ".git").stat().st_mtime_ns,
    }


def _record_conversation(
    client: str,
    payload: Mapping[str, object],
    project: Path,
    now: float,
    env: Mapping[str, str],
    proc_root: Path,
    pid: int,
) -> bool:
    """Record (or remove) the worktree a conversation starts in. Returns whether one was written."""
    conversation = _valid_id(payload.get("session_id"))
    if conversation is None or (client == "codex" and payload.get("agent_id")):
        return False  # A subagent shares its parent's conversation.
    root = _conversations_root(project)
    path = root / (hashlib.sha256(f"{client}:{conversation}".encode()).hexdigest() + ".json")
    cwd = payload.get("cwd")
    worktree = None
    if isinstance(cwd, str) and 0 < len(cwd) <= MAX_CWD and cwd.isprintable():
        worktree = _worktree_name(Path(cwd), project) if Path(cwd).is_absolute() else None
    if worktree is None or not isinstance(cwd, str):
        path.unlink(missing_ok=True)  # Resumed elsewhere: stop naming the old worktree.
        return False
    record = _conversation_record(
        client, conversation, cwd, worktree, now, project, (env, proc_root, pid)
    )
    root.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _make_room(root, project)
    # One staging name per process; the dot prefix keeps it out of every reader.
    temporary = root / f".{path.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
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


def _git_common_directory(checkout: Path) -> Path | None:
    """The Git administration directory shared by all worktrees of *checkout*."""
    marker = checkout / ".git"
    if marker.is_file():
        raw = marker.read_text(encoding="utf-8").strip()
        if not raw.startswith("gitdir: "):
            raise ValueError(f"invalid Git worktree marker: {marker}")
        admin = Path(raw.removeprefix("gitdir: "))
        marker = admin if admin.is_absolute() else checkout / admin
        common = marker / "commondir"
        if common.is_file():
            marker = marker / common.read_text(encoding="utf-8").strip()
    return marker.resolve() if marker.is_dir() else None


def _own_booley_dir(root: Path) -> Path:
    """A checkout's own Booley Directory: ``[project].dir``, else ``<root>/.booley``."""
    return _toml_booley_dir(root) or root / BOOLEY_DIR_NAME


def _main_booley_dir(selected: Path) -> Path | None:
    """The Main Booley Directory when *selected* is a linked checkout's own, else ``None``.

    Mirrors ``booley.core.booley_dir._main_booley_dir``: the owning checkout is
    the first strict ancestor with ``.git``, and only its own directory is remapped.
    """
    checkout = next((p for p in selected.absolute().parents if (p / ".git").exists()), None)
    if checkout is None:
        return None
    common = _git_common_directory(checkout)
    if common is None or common.name != ".git":
        return None
    if _own_booley_dir(checkout).resolve() != selected.resolve():
        return None
    main = _own_booley_dir(common.parent)
    if main.resolve() == selected.resolve() or not main.is_dir():
        return None
    return main


def resolve_common_booley_dir(env: Mapping[str, str], cwd: Path) -> Path | None:
    """The Main Booley Directory of the session's Project, or ``None`` outside one.

    Session activity is Common Booley State (ADR 0082): a session in a linked
    worktree writes where the Dashboard, which reads the main directory, looks.
    An unreadable Git topology keeps the selection; the hook must not fail a turn.
    """
    selected = resolve_booley_dir(env, cwd)
    if selected is None:
        return None
    try:
        return _main_booley_dir(selected) or selected
    except (OSError, ValueError, UnicodeError):
        return selected


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
        project = resolve_common_booley_dir(os.environ, Path.cwd())
        if project is not None:
            record(args[0], payload, project, now)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        # RecursionError: a deeply nested payload within the size bound.
        # stderr at exit 0 is ignored by both clients except in their debug logs.
        print(f"booley session activity not recorded: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
