"""Shared dependency-injection contracts for domain audits."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# Subprocess-compatible command runner used by environment audits; the keyword
# set varies per call, so only the return type is fixed.
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
