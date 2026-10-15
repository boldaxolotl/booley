"""Launch Codex with a process-owned tab handle while retaining its own launcher."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from booley.core.boundary import require_dict
from booley.runtime.chat_sessions import reserve_codex_chat
from booley.runtime.pid import capture_process_identity


def _command(executable: Path, handle: str) -> tuple[str, list[str]]:
    with executable.open("rb") as stream:
        magic = stream.read(4)
    if magic == b"\x7fELF":
        return str(executable), [handle, "--no-daemon"]
    package = executable.parent.parent / "package.json"
    if executable.name != "codex.js" or not package.is_file():
        raise ValueError("unrecognized Codex launcher")
    metadata = require_dict(json.loads(package.read_text(encoding="utf-8")), field="Codex package")
    if metadata.get("name") != "@openai/codex":
        raise ValueError("unrecognized Codex npm package")
    node = shutil.which("node")
    if node is None:
        raise FileNotFoundError("Codex's Node runtime was not found on PATH")
    shim = Path(__file__).parents[1] / "data" / "codex_chat.mjs"
    return node, [node, str(shim), handle, str(executable), "--no-daemon"]


def command(booley_dir: Path) -> tuple[str, list[str]]:
    """Prepare naming best-effort; unsupported launchers retain normal startup."""
    try:
        if not sys.platform.startswith("linux"):
            return "codex", ["codex"]
        installed = shutil.which("codex")
        if installed is None:
            return "codex", ["codex"]
        executable = Path(installed).resolve()
        # Discover support before reserving a handle that cannot appear on the tab.
        _command(executable, "S1")
        identity = capture_process_identity(os.getpid())
        if identity is None:
            raise ValueError("launch process identity is unavailable")
        handle = reserve_codex_chat(booley_dir, identity)
        return _command(executable, handle)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"NOTE: Codex tab handle is unavailable: {exc}", file=sys.stderr)
        return "codex", ["codex"]
