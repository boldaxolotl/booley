"""A real child process that replays a canned subprocess result."""

from __future__ import annotations

import sys

from booley.flows.base import SubprocessResult

_REPLAY = "import sys; sys.stdout.write(sys.argv[1]); sys.stderr.write(sys.argv[2]); sys.exit(int(sys.argv[3]))"


def canned_command(result: SubprocessResult) -> list[str]:
    """An argv whose process writes *result*'s stdout and stderr and exits with its code."""
    return [sys.executable, "-c", _REPLAY, result.stdout, result.stderr, str(result.returncode)]
