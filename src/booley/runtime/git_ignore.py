"""Git ignore query adapter, independent of Goal artifact eligibility policy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def ignored_paths(root: Path, paths: list[Path], *, no_index: bool = False) -> set[Path]:
    """Read bounded, NUL-safe ignore membership outside indexed submodule boundaries."""
    if not paths:
        return set()
    rows = _git(root, ["ls-files", "--stage", "-z"])
    gitlinks = {
        root / os.fsdecode(row.split(b"\t", 1)[1])
        for row in rows.split(b"\0")
        if row.startswith(b"160000 ")
    }
    selected = [path for path in paths if not any(path.is_relative_to(link) for link in gitlinks)]
    if not selected:
        return set()
    payload = b"".join(os.fsencode(path.relative_to(root).as_posix()) + b"\0" for path in selected)
    output = _git(
        root, ["check-ignore", "-z", "--stdin", *(["--no-index"] if no_index else [])], payload
    )
    return {(root / os.fsdecode(name)).resolve() for name in output.split(b"\0") if name}


def _git(root: Path, arguments: list[str], payload: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            input=payload,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("Git ignored-input classification timed out") from exc
    allowed = (0, 1) if arguments[0] == "check-ignore" else (0,)
    if result.returncode not in allowed:
        raise OSError(
            f"Git ignored-input classification failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout
