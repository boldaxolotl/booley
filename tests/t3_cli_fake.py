"""A stand-in `t3` command that names threads, logs its calls, and can be slow (POSIX only)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

_SCRIPT = """#!{python}
import json, sys, time
thread = sys.argv[3]
with open({log!r}, "a") as log:
    log.write(thread + "\\n")
time.sleep({stall})
title = {titles!r}.get(thread)
if title is None:
    print("Thread " + thread + " is no longer available.")
    sys.exit(1)
print(json.dumps({{"thread": {{"threadId": thread, "title": title}}}}))
"""


def write_t3_cli(
    directory: Path, titles: dict[str, str], *, delay_s: float = 0
) -> tuple[Path, Path]:
    """Write the CLI, which answers after *delay_s*, and return it with its call log."""
    cli, log = directory / "t3-fake", directory / "t3-calls.log"
    text = _SCRIPT.format(python=sys.executable, log=str(log), titles=titles, stall=delay_s)
    cli.write_bytes(text.encode("utf-8"))
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli, log
