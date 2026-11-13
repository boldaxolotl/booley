"""Cross-platform path/binary helpers for Booley.

Small helpers to avoid hardcoded Windows paths and make subprocess tree-kill
portable. Keeps the rest of Booley OS-agnostic without over-abstraction.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, Literal, cast, overload

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
    Sandbox at ``/work``, where backslashes are meaningless. This
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
    """Convert a Docker bind source to a native path, when host-addressable.

    Docker Desktop exposes drive binds in several POSIX forms. Daemon-private
    WSL paths have no reliable native Windows equivalent and return ``None``.
    POSIX and UNC paths otherwise pass through unchanged.
    """
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


def same_host_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Whether two host paths name the same location.

    Spelling decides first under the host's own rule: ``os.path.normcase`` folds case and
    separators only on Windows, so on Linux ``/u/Proj`` and ``/u/proj`` are different paths.
    Otherwise an alias (symlink, 8.3 name, case-insensitive volume) counts only when both
    paths exist and are the same file, so on a case-insensitive volume such as default macOS
    APFS the case variants of one directory are one location.
    """
    if os.path.normcase(left) == os.path.normcase(right):
        return True
    try:
        return Path(left).samefile(right)
    except (OSError, ValueError):  # missing path, or a NUL byte in a Docker label
        return False


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
    return dict(new_group_kwargs(is_windows=IS_WINDOWS))


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Forcibly terminate proc *and all its descendants*.

    Plain proc.kill() only reaps the direct child — EDA toolchains
    (xsim/xelab, yosys+abc, sv2v, .bat shims) spawn grandchildren that
    otherwise become orphaned zombies. Safe to call on an already-dead
    process (best-effort, swallows errors).
    """
    group = capture_process_group(proc)
    terminate_process_group(proc, group, is_windows=IS_WINDOWS)


#: Seconds ``run_captured_bounded`` waits for output pipes to close, and for its
#: stdin feeder thread to finish, after it killed the process tree.
_KILL_DRAIN_TIMEOUT_S = 5

#: Longest single blocking wait inside ``run_captured_bounded``. CPython before
#: 3.14 cannot deliver Ctrl-C to a Windows ``communicate`` blocked in one long
#: thread join, so the wait is sliced to let an interrupt through promptly.
_INTERRUPT_POLL_S = 0.5

#: A stdin/stdout stream for ``run_captured_bounded``: an open file, a file
#: descriptor, or ``subprocess.DEVNULL``.
_Stream = int | IO[Any] | None


def _decode(data: bytes | None, errors: str) -> str | None:
    """Decode captured bytes like ``Popen(text=True)``: UTF-8, universal newlines."""
    if data is None:
        return None
    return data.decode("utf-8", errors).replace("\r\n", "\n").replace("\r", "\n")


def _decode_partial(data: bytes | str | None, errors: str) -> str | None:
    """Decode the partial output of a timed-out child; ``None`` when it cannot be."""
    if isinstance(data, str) or data is None:
        return data
    try:
        return _decode(data, errors)
    except UnicodeDecodeError:
        return None


def _feed(write_fd: int, data: bytes) -> None:
    """Write *data* to the child's stdin pipe and close it (thread body).

    A broken pipe means the child, or the killed tree, is gone: nothing to do.
    The buffered writer loops until every byte is written.
    """
    with contextlib.suppress(OSError), os.fdopen(write_fd, "wb") as sink:
        sink.write(data)


def _spawn(
    argv: list[str],
    *,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    input: bytes | None,  # noqa: A002 - mirrors subprocess.run
    stdin: _Stream,
    stdout: _Stream,
) -> tuple[subprocess.Popen[bytes], threading.Thread | None]:
    """Start *argv* in its own group; return it with the stdin feeder, if any.

    Feeding *input* from a daemon thread, never through ``communicate(input=)``,
    keeps the timeout effective: CPython on Windows writes ``communicate`` input
    on the calling thread before it applies the timeout.
    """
    feed_read = feed_write = None
    if input is not None:
        feed_read, feed_write = os.pipe()
        stdin = feed_read
    try:
        # Binary pipes; the untyped group kwargs defeat Popen's overload choice.
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE if stdout is None else stdout,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                **popen_new_group_kwargs(),
            ),
        )
    except BaseException:
        if feed_write is not None:
            os.close(feed_write)
        raise
    finally:
        if feed_read is not None:
            os.close(feed_read)
    if feed_write is None or input is None:
        return proc, None
    feeder = threading.Thread(
        target=_feed, args=(feed_write, input), name="bounded-capture-input", daemon=True
    )
    feeder.start()
    return proc, feeder


def _communicate_sliced(
    proc: subprocess.Popen[bytes], timeout: float
) -> tuple[bytes | None, bytes | None]:
    """``proc.communicate(timeout=...)`` in short slices so an interrupt is prompt.

    Raises ``subprocess.TimeoutExpired`` once *timeout* seconds have passed.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        try:
            return proc.communicate(timeout=max(0.0, min(remaining, _INTERRUPT_POLL_S)))
        except subprocess.TimeoutExpired:
            if deadline - time.monotonic() <= 0:
                raise


def _close_pipes_on_posix(proc: subprocess.Popen[bytes]) -> None:
    """Close *proc*'s output pipes after an abandoned or interrupted ``communicate``.

    On POSIX ``communicate`` reads on the calling thread, so closing is safe. On
    Windows daemon reader threads own the pipes (closing a buffered pipe a reader
    is blocked on would block), so they are left to those threads.
    """
    if IS_WINDOWS:
        return
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _run_bounded(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    input: bytes | None,  # noqa: A002 - mirrors subprocess.run
    stdin: _Stream,
    stdout: _Stream,
) -> subprocess.CompletedProcess[bytes]:
    """Bytes core of :func:`run_captured_bounded`: kill the tree, bound every wait."""
    proc, feeder = _spawn(argv, cwd=cwd, env=env, input=input, stdin=stdin, stdout=stdout)
    try:
        try:
            out, err = _communicate_sliced(proc, timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=_KILL_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # A descendant escaped the group and still holds a pipe: give up
                # on its output.
                out = err = None
                _close_pipes_on_posix(proc)
            raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
        except BaseException:
            kill_process_tree(proc)
            _close_pipes_on_posix(proc)
            raise
    finally:
        if feeder is not None:
            # Abandoned when an escaped descendant still holds the pipe unread.
            feeder.join(_KILL_DRAIN_TIMEOUT_S)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


@overload
def run_captured_bounded(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    errors: str = "replace",
    input: bytes | None = None,
    stdin: _Stream = None,
    stdout: _Stream = None,
    text: Literal[True] = True,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_captured_bounded(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    errors: str = "replace",
    input: bytes | None = None,
    stdin: _Stream = None,
    stdout: _Stream = None,
    text: Literal[False],
) -> subprocess.CompletedProcess[bytes]: ...


def run_captured_bounded(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    errors: str = "replace",
    input: bytes | None = None,  # noqa: A002 - mirrors subprocess.run
    stdin: _Stream = None,
    stdout: _Stream = None,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run *argv* capturing output; never wait unboundedly on a timeout.

    Unlike ``subprocess.run(..., timeout=...)``, which kills only the direct
    child and (on Windows) then waits for the output pipes to reach EOF with no
    limit, this starts the child in its own process group, kills the whole tree
    on timeout and abandons pipes a surviving descendant still holds.
    *cwd* and *env* are the child's working directory and complete environment;
    ``None`` inherits the caller's.

    Input and output modes:

    - *input*: bytes written to the child's stdin by a daemon thread, so a child
      that never reads cannot block the timeout. Exclusive with *stdin*.
    - *stdin*: an open file, descriptor or ``subprocess.DEVNULL`` the child reads;
      ``None`` inherits the caller's.
    - *stdout*: an open file or descriptor the child writes to instead of a
      pipe; ``result.stdout`` is then ``None``. Stderr is always captured.
    - *text*: ``True`` decodes output as UTF-8 with the *errors* handler
      (``"replace"`` by default; ``"strict"`` raises ``UnicodeDecodeError`` on an
      undecodable byte) and translates newlines; ``False`` returns raw bytes. A
      timeout is always reported as ``TimeoutExpired``, even when the killed
      child's partial output no longer decodes.

    Returns the ``CompletedProcess`` for any exit status (never raises on a
    non-zero exit). Raises ``subprocess.TimeoutExpired`` after *timeout*
    seconds, and ``FileNotFoundError`` when the executable is absent.

    Worst case until ``TimeoutExpired`` is raised: about ``timeout + 12`` s on
    POSIX (SIGTERM, 2 s grace, SIGKILL, up to 5 s wait, 5 s drain) and about
    ``timeout + 32`` s on Windows, where each ``taskkill`` may take 10 s. The
    *input* thread is joined for at most 5 s more; it is abandoned when an
    escaped descendant still holds the pipe.
    """
    try:
        result = _run_bounded(
            argv, timeout=timeout, cwd=cwd, env=env, input=input, stdin=stdin, stdout=stdout
        )
    except subprocess.TimeoutExpired as expired:
        if not text:
            raise
        raise subprocess.TimeoutExpired(
            argv,
            timeout,
            output=_decode_partial(expired.output, errors),
            stderr=_decode_partial(expired.stderr, errors),
        ) from None
    if not text:
        return result
    return subprocess.CompletedProcess(
        argv, result.returncode, _decode(result.stdout, errors), _decode(result.stderr, errors)
    )
