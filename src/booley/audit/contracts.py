"""Shared dependency-injection contracts for domain audits."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# Bounded command runner injected into environment audits, shaped like
# ``run(argv, *, timeout, cwd=None) -> CompletedProcess[str]``. The audit package
# cannot import ``booley.runtime``, so composers pass
# ``booley.runtime.platform_paths.run_captured_bounded``; there is no unbounded
# default. The keyword set varies per call, so only the return type is fixed.
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
