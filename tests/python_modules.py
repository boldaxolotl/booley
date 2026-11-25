"""Control which standard modules the running Python can import."""

from __future__ import annotations

import sys
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest


def set_importable(monkeypatch: pytest.MonkeyPatch, **modules: bool) -> None:
    """Make each named module importable (True) or absent (False) for ``find_spec``."""
    for name, importable in modules.items():
        stub = ModuleType(name)
        stub.__spec__ = ModuleSpec(name, None)
        monkeypatch.setitem(sys.modules, name, stub if importable else None)
