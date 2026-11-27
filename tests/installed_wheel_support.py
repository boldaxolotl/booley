"""Redirect the stamp readers to an installed-package folder a test prepared.

The readers themselves run unchanged against the real stamp file.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from booley.runtime import build_stamp, image_build_contracts

STAMP_MODULE = "booley._build_commit"  # private: only a wheel build generates this module


def install_package(monkeypatch: pytest.MonkeyPatch, package_root: Path) -> None:
    """Make *package_root* the installed ``booley`` package the stamp readers inspect.

    Its ``_build_commit.py``, when present and valid Python, becomes ``booley._build_commit``;
    otherwise that import fails, as in a source checkout or a damaged wheel.
    """
    for module, name in (
        (build_stamp, "build_stamp.py"),
        (image_build_contracts, "image_build_contracts.py"),
    ):
        location = str(package_root / "runtime" / name)
        monkeypatch.setattr(module, "__file__", location)  # boundary: install location
    stamp: types.ModuleType | None = types.ModuleType(STAMP_MODULE)
    try:
        source = (package_root / "_build_commit.py").read_bytes()
        exec(compile(source, "_build_commit.py", "exec"), stamp.__dict__)
    except (OSError, SyntaxError, ValueError):
        stamp = None
    monkeypatch.setitem(sys.modules, STAMP_MODULE, stamp)
