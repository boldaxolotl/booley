"""When an Agent CLI's conversations were last updated, read from the CLI's own storage.

`booley chat -c` and `booley chat resume --last` pick "the most recent conversation" of the
current directory. Booley cannot know which of a main-checkout conversation and a worktree
conversation is newer from its own records, so it reads the modification times the CLIs keep:

- Claude Code: `<config>/projects/<slug(cwd)>/<id>.jsonl`, one file per conversation, where
  the slug replaces every character outside `[A-Za-z0-9]` with `-`.
- Codex: `<home>/sessions/YYYY/MM/DD/rollout-<created>-<id>.jsonl`, filed under the creation
  date and touched on every update. A rollout's first line is `session_meta`, which names the
  directory the conversation ran in.

A missing store means "no conversation" (`None`). Anything that makes the answer uncertain
raises `RecencyUnknownError`; the caller then leaves the CLI to choose, as it always did.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

from booley.core.boundary import require_dict

# Bounds on the Codex scan: files statted, and bytes of a rollout's first line.
MAX_ROLLOUTS = 16384
MAX_FIRST_LINE = 1 << 20


class RecencyUnknownError(Exception):
    """The newest conversation of a directory cannot be determined within the bounds."""


def store_root(client: str) -> Path:
    """The CLI's storage directory: its environment override, else the default in `$HOME`."""
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _claude_dir(root: Path, cwd: str) -> Path:
    return root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def last_active(client: str, cwd: str, conversation_id: str) -> float | None:
    """Modification time of one conversation's file, or ``None`` when it has none.

    *cwd* is the directory the conversation ran in. An id that is not a plain file name
    names no conversation.
    """
    if Path(conversation_id).name != conversation_id or conversation_id in ("", ".", ".."):
        return None
    root = store_root(client)
    if client == "codex":
        pattern = f"sessions/*/*/*/rollout-*-{glob.escape(conversation_id)}.jsonl"
        stamps = [stamp for path in root.glob(pattern) if (stamp := _mtime(path)) is not None]
        return max(stamps, default=None)
    return _mtime(_claude_dir(root, cwd) / f"{conversation_id}.jsonl")


def _rollout_cwd(path: Path) -> tuple[object, object]:
    """The `(cwd, source)` of a Codex rollout's `session_meta` first line.

    Raises `RecencyUnknownError` when the line is missing, over the bound or not a
    `session_meta` object.
    """
    try:
        with path.open("rb") as stream:
            line = stream.readline(MAX_FIRST_LINE + 1)
        if len(line) > MAX_FIRST_LINE:
            raise RecencyUnknownError(f"first line of {path.name} is over {MAX_FIRST_LINE} bytes")
        first = require_dict(json.loads(line))
        payload = require_dict(first.get("payload"))
        if first.get("type") != "session_meta":
            raise ValueError("not a session_meta line")
    except (OSError, ValueError, RecursionError) as exc:
        raise RecencyUnknownError(f"cannot read the first line of {path.name}: {exc}") from exc
    return payload.get("cwd"), payload.get("source")


def _newest_codex(root: Path, cwd: str, newer_than: float | None) -> float | None:
    """Update time of the newest interactive Codex rollout of *cwd* not older than *newer_than*."""
    stamped: list[tuple[float, Path]] = []
    for count, path in enumerate(root.glob("sessions/*/*/*/rollout-*.jsonl")):
        if count >= MAX_ROLLOUTS:
            raise RecencyUnknownError(f"more than {MAX_ROLLOUTS} Codex rollouts")
        stamp = _mtime(path)
        if stamp is not None and (newer_than is None or stamp >= newer_than):
            stamped.append((stamp, path))
    for stamp, path in sorted(stamped, reverse=True):
        found_cwd, source = _rollout_cwd(path)
        if found_cwd == cwd and source != "exec":
            return stamp
    return None


def latest_in(client: str, cwd: Path, newer_than: float | None) -> float | None:
    """Update time of the newest conversation of *cwd*, or ``None`` when there is none.

    For Codex only a conversation updated after *newer_than* is looked for (``None`` when
    the newest is older), which bounds how many rollouts are opened.
    """
    root = store_root(client)
    if client == "codex":
        return _newest_codex(root, str(cwd), newer_than)
    stamps = [
        stamp
        for path in _claude_dir(root, str(cwd)).glob("*.jsonl")
        if (stamp := _mtime(path)) is not None
    ]
    return max(stamps, default=None)
