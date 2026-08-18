"""Cross-platform path/binary helpers for Booley.

Small helpers to avoid hardcoded Windows paths and make subprocess tree-kill
portable. Keeps the rest of Booley OS-agnostic without over-abstraction.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


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
    return Path(os.path.relpath(path, start)).as_posix()


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
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Forcibly terminate proc *and all its descendants*.

    Plain proc.kill() only reaps the direct child — EDA toolchains
    (xsim/xelab, yosys+abc, sv2v, .bat shims) spawn grandchildren that
    otherwise become orphaned zombies. Safe to call on an already-dead
    process (best-effort, swallows errors).
    """
    if IS_WINDOWS:
        if proc.poll() is not None:
            return
        # taskkill /T walks the child-tree, /F is forceful.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        # Fallback: direct kill in case taskkill isn't on PATH.
        with contextlib.suppress(OSError):
            proc.kill()
    else:
        import signal

        # popen_new_group_kwargs() makes the child the leader of a fresh
        # session, so its PID remains the process-group ID even if the wrapper
        # exits before one of its simulator grandchildren.  Do not derive the
        # PGID with getpgid(proc.pid): that fails in precisely that orphan race.
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
            return

        # Give the group a brief moment to exit cleanly, then kill any
        # descendants that ignored SIGTERM. Waiting only for the direct child
        # is insufficient: make/python can exit while Vtop keeps running.
        if proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except OSError:
            pass
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
