"""Concurrent pipe observation preserving text-mode subprocess captures."""

from __future__ import annotations

import codecs
import io
import os
import select
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from booley.runtime.platform_paths import kill_process_tree

if TYPE_CHECKING:
    from booley.flows.terminal_progress import TerminalProgress


def _windows_pipe_ready(fd: int) -> bool:
    """Poll anonymous pipes without the Python 3.12 nonblocking prerequisite."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    available = wintypes.DWORD()
    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    peek.restype = wintypes.BOOL
    if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == 109:  # ERROR_BROKEN_PIPE: let os.read consume EOF.
            return True
        raise ctypes.WinError(error)
    return available.value > 0


class _Capture:
    def __init__(self, pipe, name: str, observer: TerminalProgress) -> None:
        self.pipe, self.name, self.observer = pipe, name, observer
        decoder = codecs.getincrementaldecoder(pipe.encoding)(pipe.errors)
        self.decoder = io.IncrementalNewlineDecoder(decoder, translate=True)
        self.parts: list[str] = []
        self.error: BaseException | None = None
        self.stop = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _read(self) -> bytes | None:
        if os.name != "nt":
            ready, _, _ = select.select([self.pipe.fileno()], [], [], 0.1)
            if not ready:
                return None
            return os.read(self.pipe.fileno(), 65536)
        if not _windows_pipe_ready(self.pipe.fileno()):
            self.stop.wait(0.02)
            return None
        return os.read(self.pipe.fileno(), 65536)

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                chunk = self._read()
                if chunk is None:
                    continue
                if not chunk:
                    break
                self.observer.observe(self.name, chunk)
                self.parts.append(self.decoder.decode(chunk))
            self.parts.append(self.decoder.decode(b"", final=True))
        except (OSError, UnicodeError, ValueError) as exc:
            self.error = exc
        finally:
            self.done.set()

    def text(self) -> str:
        return "".join(self.parts)


def _wait(process, captures: list[_Capture], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for capture in captures:
            if capture.error is not None:
                raise capture.error
        if process.poll() is not None and all(capture.done.is_set() for capture in captures):
            return True
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    return False


def _shutdown(process, captures: list[_Capture]) -> None:
    kill_process_tree(process)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(item.done.is_set() for item in captures):
        time.sleep(0.02)
    if process.poll() is None:
        process.kill()
    for capture in captures:
        capture.stop.set()
    for capture in captures:
        capture.thread.join(timeout=0.5)
    process.wait(timeout=1)


def communicate_observed(
    process: subprocess.Popen,
    observer: TerminalProgress,
    *,
    timeout: float,
    on_capture_complete: Callable[[], None] | None = None,
) -> tuple[str, str, bool]:
    """Drain both pipes with sole reader ownership and bounded killed drainage.

    The Popen text wrappers supply the existing locale, strict decoding and
    universal-newline contract. Readers consume their binary buffers instead;
    only the separate human presentation decoder replaces invalid characters.
    """
    captures = [
        _Capture(process.stdout, f"stdout:{process.pid}", observer),
        _Capture(process.stderr, f"stderr:{process.pid}", observer),
    ]
    for capture in captures:
        capture.thread.start()
    try:
        completed = _wait(process, captures, timeout)
        if on_capture_complete is not None:
            on_capture_complete()
        if not completed:
            _shutdown(process, captures)
        for capture in captures:
            if capture.error is not None:
                raise capture.error
        return captures[0].text(), captures[1].text(), not completed
    except BaseException:
        _shutdown(process, captures)
        raise
    finally:
        for capture in captures:
            capture.stop.set()
            capture.thread.join(timeout=0.5)
        observer.end_command()
