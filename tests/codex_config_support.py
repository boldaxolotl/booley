"""Shared helpers for the Codex config writer tests."""

from __future__ import annotations

import tomllib
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest


@contextmanager
def count_parses(monkeypatch: pytest.MonkeyPatch, source: str) -> Iterator[Any]:
    """Yield a callable counting every tomlkit or stdlib parse of exactly *source*."""
    import tomlkit.parser

    seen: list[str] = []
    real_init = tomlkit.parser.Parser.__init__
    real_loads = tomllib.loads

    def counted_init(self: Any, string: str) -> None:
        seen.append(string)
        real_init(self, string)

    def counted_loads(text: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(tomlkit.parser.Parser, "__init__", counted_init)
    monkeypatch.setattr(tomllib, "loads", counted_loads)
    yield lambda: seen.count(source)


def tomlkit_copy(version: str) -> types.ModuleType:
    """A working stand-in for tomlkit that reports *version*."""
    import tomlkit

    module = types.ModuleType("tomlkit")
    module.__dict__.update(tomlkit.__dict__)
    module.__version__ = version
    return module
