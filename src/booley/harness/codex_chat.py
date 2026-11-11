"""Launch Codex with a process-owned tab handle while retaining its own launcher."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.runtime.chat_sessions import CODEX_NATIVE_NAMES, reserve_codex_chat
from booley.runtime.pid import capture_process_identity


def _command(executable: Path) -> tuple[str, list[str], list[str]]:
    """Classify the installed Codex by file name only.

    Returns the program, the argv before the handle and the argv after it.
    """
    if executable.name in CODEX_NATIVE_NAMES:
        return str(executable), [], ["--no-daemon"]
    if executable.name != "codex.js":
        raise ValueError("unrecognized Codex launcher")
    node = shutil.which("node")
    if node is None:
        raise FileNotFoundError("Codex's Node runtime was not found on PATH")
    if Path(node).resolve().name not in {"node", "nodejs"}:
        raise ValueError("unrecognized Node runtime; launcher shims cannot own a tab")
    shim = Path(__file__).parents[1] / "data" / "codex_chat.mjs"
    return node, [node, str(shim)], [str(executable), "--no-daemon"]


class NamingUnsupportedError(ValueError):
    """This host or Codex install cannot carry a Booley tab handle; plain Codex still starts."""


def _named_command(
    project_root: Path, wait_s: float | None, predecessors: tuple[str, ...]
) -> tuple[str, list[str], str]:
    """Reserve the handle and build the named command; raise when naming is unavailable."""
    if not sys.platform.startswith("linux"):
        raise NamingUnsupportedError("Codex tab handles need Linux")
    installed = shutil.which("codex")
    if installed is None:
        raise NamingUnsupportedError("codex was not found on PATH")
    # Discover support before reserving a handle that cannot appear on the tab.
    program, before, after = _command(Path(installed).resolve())
    identity = capture_process_identity(os.getpid())
    if identity is None:
        raise ValueError("launch process identity is unavailable")
    handle = reserve_codex_chat(
        resolve_common_booley_dir(project_root),
        identity,
        wait_s=wait_s,
        predecessors=predecessors,
    )
    return program, [*before, handle, *after], handle


def command(
    project_root: Path,
    *,
    wait_s: float | None = None,
    strict: bool = False,
    predecessors: tuple[str, ...] = (),
) -> tuple[str, list[str], str | None]:
    """Return the program, its argv and the reserved handle (``None`` when unnamed).

    Naming is best effort: an unsupported launcher retains normal startup and an error
    adds a NOTE. With *strict*, every reason the handle cannot be reserved is raised
    instead, for a caller that cannot start without one. *wait_s* bounds the wait for
    the Session writer lock. A resumed tab keeps the handle of its *predecessors*.
    """
    try:
        return _named_command(project_root, wait_s, predecessors)
    except NamingUnsupportedError:
        if strict:
            raise
    except (OSError, ValueError, RuntimeError) as exc:
        if strict:
            raise
        print(f"NOTE: Codex tab handle is unavailable: {exc}", file=sys.stderr)
    return "codex", ["codex"], None
