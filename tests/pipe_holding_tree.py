"""Real-process fixtures for timeout tests: a child tree that holds its output pipes.

``PARENT_SCRIPT`` starts a heartbeat grandchild that inherits the parent's
stdout/stderr, so a captured-command helper that waits on the pipes cannot finish
by itself.  Shared by the ``_run_docker``, session-runtime and admission tests.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Parent script: starts a heartbeat grandchild that inherits stdout/stderr, then
# either sleeps (parent still alive at the timeout; ``live-until-stop`` ends at the stop file) or exits at once (the Docker
# CLI is gone but a descendant still holds the output pipe).  Mode ``escape``
# also puts the grandchild in its own session (POSIX), out of reach of the
# process-group kill, so only abandoning the pipes can bound the call.  The grandchild
# ends itself when the test creates the stop file (or after ~40 s) and then
# writes the done file, so the tests never kill a recorded PID (which could have
# been reused) yet leave no process behind.
PARENT_SCRIPT = """\
import os, subprocess, sys, time
heartbeat, stop, done, mode = sys.argv[1:5]
child = (
    "import os, time\\n"
    "end = time.monotonic() + 40\\n"
    "while time.monotonic() < end and not os.path.exists({stop!r}):\\n"
    "    open({heartbeat!r}, 'a').write('x')\\n"
    "    time.sleep(0.05)\\n"
    "open({done!r}, 'w').close()\\n"
).format(heartbeat=heartbeat, stop=stop, done=done)
# Pass the std streams explicitly: on Windows a Popen without them inherits no
# handles, so the grandchild would not hold the parent's output pipe at all.
subprocess.Popen(
    [sys.executable, "-c", child],
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=(mode == "escape"),
)
if mode == "live":
    time.sleep(40)
if mode == "live-until-stop":
    # Like ``live``, but also ends with the grandchild so a test that cannot kill
    # the tree (the code under test is the bug) leaves no process behind.
    end = time.monotonic() + 40
    while time.monotonic() < end and not os.path.exists(stop):
        time.sleep(0.05)
"""


@contextmanager
def heartbeat_tree(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Paths for the heartbeat grandchild; leaving the block stops it and waits."""
    paths = {name: tmp_path / name for name in ("heartbeat", "stop", "done")}
    try:
        yield paths
    finally:
        paths["stop"].touch()
        deadline = time.monotonic() + 5
        while not paths["done"].exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def parent_argv(tmp_path: Path, python: str, paths: dict[str, Path], mode: str) -> list[str]:
    """Argv that runs ``PARENT_SCRIPT`` with *python* in the given *mode*."""
    script = tmp_path / "parent.py"
    script.write_text(PARENT_SCRIPT, encoding="utf-8")
    return [
        python,
        str(script),
        str(paths["heartbeat"]),
        str(paths["stop"]),
        str(paths["done"]),
        mode,
    ]


def wait_until_heartbeat_stops(heartbeat: Path, *, window: float = 10) -> bool:
    """Poll until the heartbeat file stops growing; False when it never does."""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        before = heartbeat.stat().st_size
        time.sleep(0.5)
        if heartbeat.stat().st_size == before:
            return True
    return False
