"""Simulate Windows file-sharing violations on POSIX for the retrying readers."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest


def sharing_error(winerror: int = 32) -> PermissionError:
    """A ``PermissionError`` shaped like a Windows sharing violation."""
    error = PermissionError(13, "The process cannot access the file")
    error.winerror = winerror  # type: ignore[attr-defined]
    return error


def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the retry budget to two opens without sleeping for real."""
    monkeypatch.setattr(
        "booley.runtime.filesystem_utils._SHARING_RETRY_DELAYS_SECONDS", (0.0,), raising=False
    )


def hold_opens(
    monkeypatch: pytest.MonkeyPatch, held: dict[Path, int], *, skip: int = 0
) -> Counter[Path]:
    """Fail opens of each path in ``held`` with a sharing violation.

    The first ``skip`` opens of a path succeed, the next ``held[path]`` fail, and
    later ones succeed. Returns a counter of every open attempt per path.
    """
    no_retry_delay(monkeypatch)
    attempts: Counter[Path] = Counter()
    real_open = Path.open

    def open_with_holds(path: Path, *args: object, **kwargs: object):
        if path in held:
            attempts[path] += 1
            if skip < attempts[path] <= skip + held[path]:
                raise sharing_error()
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", open_with_holds)
    return attempts
