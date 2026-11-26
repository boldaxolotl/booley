"""A fake of the host operating system for Booley code that branches on it.

The operating system is an external boundary. Booley reads it in three ways, so this module
has one function for each. Each patches nothing when the requested platform is the real
host's, so a test of the host's own platform runs unpatched.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from booley.runtime import platform_paths

HOST_IS_WINDOWS = sys.platform == "win32"


def use_windows_paths(monkeypatch: pytest.MonkeyPatch, *, windows: bool) -> None:
    """Make code that reads ``platform_paths.IS_WINDOWS`` see a Windows or POSIX host."""
    if windows != HOST_IS_WINDOWS:
        monkeypatch.setattr(platform_paths, "IS_WINDOWS", windows)


def use_sys_platform(monkeypatch: pytest.MonkeyPatch, *, windows: bool) -> None:
    """Make code that reads ``sys.platform`` at call time see a Windows or POSIX host.

    Keep the patched block short: the standard library reads ``sys.platform`` too
    (``shutil.which`` calls ``_winapi`` when it says ``win32``).
    """
    if windows != HOST_IS_WINDOWS:
        monkeypatch.setattr(sys, "platform", "win32" if windows else "linux")


def use_windows_deadline_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select ``run_captured_bounded``'s Windows deadline policy on any host.

    Only ``platform_paths`` sees ``win32``; process-group operations stay native.
    """
    monkeypatch.setattr(platform_paths, "sys", SimpleNamespace(platform="win32"))
