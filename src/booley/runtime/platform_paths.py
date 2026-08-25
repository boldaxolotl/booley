"""Cross-platform path/binary helpers for Booley.

Small helpers to avoid hardcoded Windows paths and make subprocess tree-kill
portable. Keeps the rest of Booley OS-agnostic without over-abstraction.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from booley.runtime.process_group import (
    capture_process_group,
    new_group_kwargs,
    terminate_process_group,
)

IS_WINDOWS = sys.platform == "win32"
_WINDOWS_DOCKER_DRIVE_RE = re.compile(
    r"^/(?:host_mnt/|run/desktop/mnt/host/)?(?P<drive>[A-Za-z])(?P<suffix>/.*)?$"
)


def posix_relpath(path: Path | str, start: Path | str) -> str:
    """``os.path.relpath`` with a guaranteed POSIX-separated result.

    ``os.path.relpath`` renders OS-native separators, so on a Windows host it
    yields ``..\\rtl\\dut.sv``. Every relative path Booley composes here is
    *container-destined* — it lands in an edalize ``.vc``/Makefile, a ``make``
    command, a filelist, or a run-log pointer that is consumed inside the Linux
    Session Runtime at ``/work``, where backslashes are meaningless. This
    normalizes to ``/`` on every host so the same string crosses the boundary
    unchanged.
    """
    try:
        relative = os.path.relpath(path, start)
    except ValueError:
        # Windows cannot express a relative path between drive letters. This
        # occurs for host-side reports when the checkout and runner temp
        # directory live on different drives (GitHub uses D: and C:). Keep the
        # pointer usable and slash-normalized instead of crashing the Flow.
        return Path(path).resolve().as_posix()
    return Path(relative).as_posix()


def docker_mount_path(p: Path) -> str:
    """Convert host path to Docker bind-mount format.

    On Windows, ``C:\\foo\\bar`` becomes ``/c/foo/bar``.
    On other platforms, returns the POSIX path unchanged.
    """
    posix = p.as_posix()
    if not IS_WINDOWS:
        return posix
    if len(posix) >= 2 and posix[1] == ":":
        return "/" + posix[0].lower() + posix[2:]
    return posix


def host_path_from_docker_mount(value: str) -> Path | None:
    """Convert a Docker bind source to a native path, if it is host-addressable."""
    if IS_WINDOWS:
        match = _WINDOWS_DOCKER_DRIVE_RE.fullmatch(value)
        if match:
            suffix = match.group("suffix") or "/"
            return Path(f"{match.group('drive').upper()}:{suffix}")
        if value.startswith("/") and not value.startswith("//"):
            # Docker Desktop can retain daemon-private WSL paths that native
            # Windows cannot inspect. Their existence is therefore unknown.
            return None
    return Path(value)


# MSYS2 fallback path (used only on Windows when cargo is not on PATH).
# Use native Windows path so Path.exists() works under both MSYS2 and
# native Python (bash-style /c/... only resolves inside MSYS2 shell).
_MSYS2_CARGO = Path("C:/msys64/mingw64/bin/cargo.exe") if IS_WINDOWS else Path("/nonexistent")


def venv_python(venv_dir: Path) -> Path:
    """Return the Python interpreter path inside a venv.

    Windows venvs use Scripts/python.exe; POSIX venvs use bin/python.
    """
    venv_dir = Path(venv_dir)
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def bash_bin() -> str:
    """Locate a real bash (MSYS2 / Git Bash). Skip WSL shims on Windows."""
    if IS_WINDOWS:
        # WSL shims live in System32 and WindowsApps — avoid them.
        _WSL_DIRS = {"system32", "windowsapps"}
        for candidate in (
            Path("C:/msys64/usr/bin/bash.exe"),
            Path("C:/Program Files/Git/usr/bin/bash.exe"),
            Path("C:/Program Files/Git/bin/bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
        # Fall back to PATH, filtering out WSL directories
        found = shutil.which("bash")
        if found:
            parent = Path(found).resolve().parent.name.lower()
            if parent not in _WSL_DIRS:
                return found
    return shutil.which("bash") or "bash"


def cargo_bin() -> str:
    """Locate cargo executable. Prefer PATH; on Windows, fall back to MSYS2."""
    found = shutil.which("cargo")
    if found:
        return found
    if IS_WINDOWS and _MSYS2_CARGO.exists():
        return str(_MSYS2_CARGO)
    # Last resort: plain name, let the caller surface the ENOENT.
    return "cargo"


def native_binary(base_path: Path, name: str) -> Path:
    """Return base_path/name with .exe suffix on Windows."""
    suffix = ".exe" if IS_WINDOWS else ""
    return Path(base_path) / (name + suffix)


# ----------------------------------------------------------------------
# Process-tree spawn / kill (for EDA tool wrappers that fork grandchildren)
# ----------------------------------------------------------------------


def popen_new_group_kwargs() -> dict:
    """Extra Popen/subprocess.run kwargs to put the child in its own group.

    On Windows: CREATE_NEW_PROCESS_GROUP so we can target the whole tree via
    taskkill /T. On POSIX: start_new_session=True so killpg() reaches all
    descendants (xsim, xelab, yosys-abc, sv2v, .bat-spawned children, ...).
    """
    return new_group_kwargs(is_windows=IS_WINDOWS)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Forcibly terminate proc *and all its descendants*.

    Plain proc.kill() only reaps the direct child — EDA toolchains
    (xsim/xelab, yosys+abc, sv2v, .bat shims) spawn grandchildren that
    otherwise become orphaned zombies. Safe to call on an already-dead
    process (best-effort, swallows errors).
    """
    group = capture_process_group(proc)
    terminate_process_group(proc, group, is_windows=IS_WINDOWS)
