"""Fake ``BOOLEY_GATEWAY_TOKEN_COMMAND`` programs for the gateway tests (#2267).

Each fake is a Python script run through ``sys.executable``. It appends one
line to a counter file per run, so a test counts command runs at the process
boundary instead of through Booley's internals.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

_SCRIPT = """\
import json, sys

spec = json.loads({spec})
with open(spec["counter"], "a", encoding="utf-8") as counter:
    counter.write("run\\n")
with open(spec["counter"], encoding="utf-8") as counter:
    runs = len(counter.read().splitlines())
sys.stderr.write(spec["stderr"])
sys.stdout.write(spec["tokens"][min(runs, len(spec["tokens"])) - 1])
sys.exit(spec["exit_code"])
"""


def write_token_command(
    directory: Path,
    tokens: list[str],
    *,
    stderr: str = "",
    exit_code: int = 0,
    extra_argument: str | None = None,
) -> tuple[str, Path]:
    """Write a fake token command; return ``(command string, counter file)``.

    Run N prints ``tokens[N-1]`` (the last token repeats). The command string is
    built with ``shlex.join`` so no raw OS path is embedded in a literal.
    """
    counter = directory / "command-runs.txt"
    spec = {"counter": str(counter), "tokens": tokens, "stderr": stderr, "exit_code": exit_code}
    script = directory / "token_command.py"
    script.write_text(_SCRIPT.format(spec=repr(json.dumps(spec))), encoding="utf-8")
    argv = [sys.executable, str(script)]
    if extra_argument is not None:
        argv.append(extra_argument)
    return shlex.join(argv), counter


def count_runs(counter: Path) -> int:
    """How many times a fake command (or fake Codex) appended to *counter*."""
    if not counter.exists():
        return 0
    return len(counter.read_text(encoding="utf-8").splitlines())
