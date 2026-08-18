"""Booley Console — full-screen TUI for the harness.

Auto-activates on TTY, falls back to log mode on non-TTY or crash.
"""

from __future__ import annotations

from .app import ConsoleApp

__all__ = ["ConsoleApp"]
