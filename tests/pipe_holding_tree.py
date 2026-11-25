"""Real-process fixtures for timeout tests: a child tree that holds its output pipes.

``PARENT_SCRIPT`` starts a heartbeat grandchild that inherits the parent's
stdout/stderr, so a captured-command helper that waits on the pipes cannot finish
by itself.  Shared by the ``_run_docker``, session-runtime and admission tests, the
``run_captured_bounded`` mode tests, the Windows Git process probe and the post-setup hook tests.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.time_budget import hang_guard_s, scaled

# How long the tree lives when nothing stops it.  It must outlive a caller's whole
# observation: the largest caller bound (``scaled(40)``) plus ``HEARTBEAT_WINDOW_S``
# plus one 0.5 s sample, or a grandchild that escaped a kill would stop by itself
# inside the window and read as killed.
TREE_LIFE_S = scaled(60)
# Window in which ``wait_until_heartbeat_stops`` looks for the heartbeat to stop.
HEARTBEAT_WINDOW_S = scaled(10)

# Parent script: starts a heartbeat grandchild that inherits stdout/stderr, then
# either sleeps (parent still alive at the timeout; ``live-until-stop`` ends at
# the stop file) or exits at once (the Docker
# CLI is gone but a descendant still holds the output pipe).  Mode ``escape``
# also puts the grandchild in its own session (POSIX), out of reach of the
# process-group kill, so only abandoning the pipes can bound the call.  The grandchild
# ends itself when the test creates the stop file (or after its life, ``TREE_LIFE_S``) and then
# writes the done file, so the tests never kill a recorded PID (which could have
# been reused) yet leave no process behind.
PARENT_SCRIPT = """\
import os, subprocess, sys, time
heartbeat, stop, done, mode = sys.argv[1:5]
child = (
    "import os, time\\n"
    "end = time.monotonic() + TREE_LIFE_S\\n"
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
    time.sleep(TREE_LIFE_S)
if mode == "live-until-stop":
    # Like ``live``, but also ends with the grandchild so a test that cannot kill
    # the tree (the code under test is the bug) leaves no process behind.
    end = time.monotonic() + TREE_LIFE_S
    while time.monotonic() < end and not os.path.exists(stop):
        time.sleep(0.05)
""".replace("TREE_LIFE_S", repr(TREE_LIFE_S))


@contextmanager
def heartbeat_tree(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Paths for the heartbeat grandchild; leaving the block stops it and waits."""
    paths = {name: tmp_path / name for name in ("heartbeat", "stop", "done")}
    try:
        yield paths
    finally:
        paths["stop"].touch()
        _wait_for_tree_exit(paths)


def _wait_for_tree_exit(paths: dict[str, Path]) -> None:
    """Wait for the done file or for a quiet heartbeat after a likely kill.

    A live grandchild writes every 0.05 s and ends within one beat of the stop
    file; a killed one never writes the done file.
    """
    deadline = time.monotonic() + hang_guard_s()
    last_size, last_change = _heartbeat_size(paths["heartbeat"]), time.monotonic()
    while not paths["done"].exists() and time.monotonic() < deadline:
        time.sleep(0.05)
        size = _heartbeat_size(paths["heartbeat"])
        if size != last_size:
            last_size, last_change = size, time.monotonic()
        elif time.monotonic() - last_change > scaled(1):
            return


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


def wait_until_heartbeat_stops(heartbeat: Path, *, window: float = HEARTBEAT_WINDOW_S) -> bool:
    """Poll until the heartbeat file stops growing; False when it never does."""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        before = heartbeat.stat().st_size
        time.sleep(0.5)
        if heartbeat.stat().st_size == before:
            return True
    return False


def _heartbeat_size(heartbeat: Path) -> int:
    """Size of the heartbeat file; 0 while it does not exist."""
    try:
        return heartbeat.stat().st_size
    except FileNotFoundError:
        return 0


def describe_heartbeat_tree(paths: dict[str, Path]) -> str:
    """What a heartbeat tree is doing now, for a failure message; polls for up to 1 s."""
    before = _heartbeat_size(paths["heartbeat"])
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if _heartbeat_size(paths["heartbeat"]) > before:
            return "was still alive (heartbeat growing)"
    if paths["done"].exists():
        return f"had exited on its own (done file present, its {TREE_LIFE_S:g} s limit)"
    return "showed no heartbeat for 1 s and no done file (killed, or alive but not scheduled)"


def stall_wait_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bounded capture's own child ignore its communicate timeout.

    The stall occurs outside Booley inside CPython's subprocess wait.
    """
    from booley.runtime import platform_paths

    # private: the stall is reproduced at the spawn seam, below any public entry.
    real_spawn = platform_paths._spawn

    def spawn(*args, **kwargs):
        proc, feeder = real_spawn(*args, **kwargs)
        real_communicate = proc.communicate
        monkeypatch.setattr(proc, "communicate", lambda **_: real_communicate())
        return proc, feeder

    monkeypatch.setattr(platform_paths, "_spawn", spawn)


def wait_for_tree_start(paths: dict[str, Path]) -> float:
    """Wait for the heartbeat grandchild's first write; return that ``time.monotonic()``.

    Fails the test when no heartbeat appears within the hang guard.
    """
    deadline = time.monotonic() + hang_guard_s()
    while not paths["heartbeat"].exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"the process tree never started: no heartbeat in {hang_guard_s()} s")
        time.sleep(0.05)
    return time.monotonic()


def start_deadline_after_tree_start(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> list[float]:
    """Hold the bounded runner's deadline until the heartbeat tree has started.

    The runner starts its timeout when the spawn returns, so a slow host cannot
    kill the tree before it exists.  The returned list receives the moment the
    tree started; measure elapsed time from it.
    """
    from booley.runtime import platform_paths

    # private: the spawn seam is the only point between the start and the deadline.
    real_spawn = platform_paths._spawn
    started: list[float] = []

    def spawn(*args, **kwargs):
        proc, feeder = real_spawn(*args, **kwargs)
        try:
            started.append(wait_for_tree_start(paths))
        except BaseException:
            with proc:  # closes its pipes and reaps it
                platform_paths.kill_process_tree(proc)
            raise
        return proc, feeder

    monkeypatch.setattr(platform_paths, "_spawn", spawn)
    return started
