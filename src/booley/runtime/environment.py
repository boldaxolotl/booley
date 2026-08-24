"""Scoped mutation of the process environment."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

_MISSING = object()


@contextmanager
def scoped_environment(overrides: Mapping[str, str | None]) -> Iterator[None]:
    """Apply *overrides* and restore every affected key on exit."""
    previous = {key: os.environ.get(key, _MISSING) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
