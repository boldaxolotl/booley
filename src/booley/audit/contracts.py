"""Shared dependency-injection contracts for domain audits."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any, Protocol


class CommandRunner(Protocol):
    """Subprocess-compatible command runner used by environment audits."""

    def __call__(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]: ...
