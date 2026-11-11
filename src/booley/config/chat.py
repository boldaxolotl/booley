"""Validated `[chat]` configuration: where `booley chat` starts its agent."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict

CHAT_WORKTREE_MODES = ("auto", "always", "never")
DEFAULT_CHAT_WORKTREE = "auto"


def chat_worktree(booley_dir: Path) -> str:
    """Read `[chat].worktree` from the Booley Directory's `booley.toml` (default `auto`)."""
    path = booley_dir / "booley.toml"
    if not path.exists():
        return DEFAULT_CHAT_WORKTREE
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_chat_worktree(data)


def parse_chat_worktree(data: Mapping[str, Any]) -> str:
    """Validate the `[chat]` table without filesystem access; return the worktree mode."""
    section = require_dict(data.get("chat", {}), field="[chat]")
    value = section.get("worktree", DEFAULT_CHAT_WORKTREE)
    if value not in CHAT_WORKTREE_MODES:
        raise ValueError("[chat].worktree must be one of " + ", ".join(CHAT_WORKTREE_MODES))
    return str(value)
