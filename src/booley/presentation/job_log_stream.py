"""Terminal stream that copies a Standalone Job's output to ``job.log``."""

from __future__ import annotations

import contextlib
import io
import threading
from typing import BinaryIO, TextIO

from booley.presentation.colors import strip_ansi


class JobLogStream(io.TextIOBase):
    """A text stream that shows everything on *original* and copies it to ``job.log``.

    The terminal sees the exact same writes. The log gets the text without ANSI
    styling. Terminal identity (``isatty``, ``fileno``, encoding) is the original's,
    so progress rendering and color decisions do not change.
    """

    def __init__(self, original: TextIO, log: BinaryIO) -> None:
        super().__init__()
        self._original = original
        self._log: BinaryIO | None = log
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]  # read-only view of the terminal's
        return self._original.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]  # read-only view of the terminal's
        return self._original.errors

    def write(self, text: str, /) -> int:
        written = self._original.write(text)
        self.log_only(text)
        return written

    def log_only(self, text: str) -> None:
        """Append *text* to ``job.log`` without touching the terminal."""
        with self._lock:
            if self._log is None:
                return
            try:
                self._log.write(strip_ansi(text).encode("utf-8", errors="replace"))
            except OSError:
                self._log = None  # a full disk must not fail the run

    def flush(self) -> None:
        # Finalization flushes too, by which time the terminal may be closed.
        with contextlib.suppress(ValueError):
            self._original.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    def fileno(self) -> int:
        return self._original.fileno()

    def writable(self) -> bool:
        return True
