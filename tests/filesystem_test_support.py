"""Shared fakes for filesystem read-retry tests."""

from __future__ import annotations

from pathlib import Path

import pytest


class FlakyOpen:
    """Stand-in for Path.open that fails the first reads of one path."""

    def __init__(self, target: Path, failures: list[OSError]) -> None:
        self.target = target
        self.failures = failures
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open = Path.open

        # A plain function (not this instance) so Path binds it as a method.
        def flaky_open(path: Path, *args: object, **kwargs: object):
            if path == self.target:
                self.calls += 1
                if self.failures:
                    raise self.failures.pop(0)
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", flaky_open)
