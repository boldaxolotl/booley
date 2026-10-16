"""Clickable Dashboard paths: mark published paths and open one in the editor."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rich.style import Style
from rich.text import Text

from booley.config.editor import ResolvedEditor, resolve_editor

OPEN_ACTION = "app.open_path"


def linked_text(detail: str | Text, paths: Iterable[str]) -> Text:
    """Make each whole line that is a published path clickable; other text stays inert.

    A path is only linked on a line of its own, which also keeps it unwrapped
    for a terminal's own link detector.
    """
    text = detail.copy() if isinstance(detail, Text) else Text(detail)
    published = set(paths)
    offset = 0
    for line in text.plain.split("\n"):
        if line in published:
            text.stylize(
                Style(underline=True, meta={"@click": f"{OPEN_ACTION}({line!r})"}),
                offset,
                offset + len(line),
            )
        offset += len(line) + 1
    return text


def open_in_editor(
    path: str,
    *,
    editor: ResolvedEditor | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
) -> str:
    """Open *path* in the installed editor, detached. Returns "" or a short failure hint.

    Opening a file is navigation: it never starts, cancels or changes work.
    """
    if not Path(path).is_absolute():
        return f"Not an absolute path: {path}"
    resolved = editor or resolve_editor()
    if resolved is None:
        return "No supported editor command on PATH; copy the path instead"
    argv = [token.replace("{file}", path) for token in resolved.open]
    # The editor must outlive the Dashboard and must not share its terminal.
    detach: dict[str, Any] = (
        {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach,
        )
    except OSError as exc:
        return f"Editor launch failed: {exc}"
    return ""
