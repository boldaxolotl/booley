"""Simulate Windows file-sharing violations on POSIX for the retrying readers."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

T = TypeVar("T")


def sharing_error(winerror: int = 32) -> PermissionError:
    """A ``PermissionError`` shaped like a Windows sharing violation."""
    error = PermissionError(13, "The process cannot access the file")
    error.winerror = winerror  # type: ignore[attr-defined]
    return error


def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the retry budget to two opens without sleeping for real."""
    monkeypatch.setattr("booley.core.sharing_retry._SHARING_RETRY_DELAYS_SECONDS", (0.0,))


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


def hold_os_opens(monkeypatch: pytest.MonkeyPatch, held: dict[Path, int]) -> Counter[Path]:
    """Fail ``os.open`` of each path in ``held`` with a sharing violation.

    For the no-follow readers, which open through ``os.open`` rather than
    ``Path.open``. The first ``held[path]`` opens of a path fail, later ones
    succeed; other paths and non-path arguments call through. Returns a counter
    of every open attempt per path.
    """
    no_retry_delay(monkeypatch)
    attempts: Counter[Path] = Counter()
    real_open = os.open

    def open_with_holds(file: object, *args: object, **kwargs: object):
        if isinstance(file, str | os.PathLike) and Path(file) in held:
            attempts[Path(file)] += 1
            if attempts[Path(file)] <= held[Path(file)]:
                raise sharing_error()
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_with_holds)
    return attempts


def hold_unlinks(monkeypatch: pytest.MonkeyPatch, held: dict[Path, int]) -> Counter[Path]:
    """Fail ``Path.unlink`` of each path in ``held`` with a sharing violation.

    The first ``held[path]`` unlinks of a path fail, later ones succeed.
    Returns a counter of every unlink attempt per path.
    """
    no_retry_delay(monkeypatch)
    attempts: Counter[Path] = Counter()
    real_unlink = Path.unlink

    def unlink_with_holds(path: Path, missing_ok: bool = False):
        if path in held:
            attempts[path] += 1
            if attempts[path] <= held[path]:
                raise sharing_error()
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_with_holds)
    return attempts


def hold_replaces(monkeypatch: pytest.MonkeyPatch, held: dict[Path, int]) -> Counter[Path]:
    """Fail ``Path.replace`` onto each target in ``held`` with a sharing violation.

    The first ``held[target]`` replaces onto a target fail, later ones succeed.
    Returns a counter of every replace attempt per target.
    """
    no_retry_delay(monkeypatch)
    attempts: Counter[Path] = Counter()
    real_replace = Path.replace

    def replace_with_holds(path: Path, target: object):
        destination = Path(target)  # type: ignore[arg-type]
        if destination in held:
            attempts[destination] += 1
            if attempts[destination] <= held[destination]:
                raise sharing_error()
        return real_replace(path, destination)

    monkeypatch.setattr(Path, "replace", replace_with_holds)
    return attempts


def flaky(function: Callable[..., T], failures: int) -> tuple[Callable[..., T], list[int]]:
    """Wrap ``function`` so its first ``failures`` calls raise a sharing violation.

    Returns the wrapper and a one-item list holding the number of calls so far.
    """
    calls = [0]

    def wrapper(*args: object, **kwargs: object) -> T:
        calls[0] += 1
        if calls[0] <= failures:
            raise sharing_error()
        return function(*args, **kwargs)

    return wrapper, calls
