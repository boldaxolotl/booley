"""A Dashboard nobody renders any more does not block a new one; a visible one still does.

Each test runs a real Dashboard (or a stand-in socket owner) as a child process and
decides ownership through the same Sandbox socket the product uses. The "terminal" of
a Dashboard child is a PTY held by the test: it either answers the Dashboard's
cursor-position heartbeat like an attached terminal emulator (seen) or never does,
like a pty whose browser tab was reloaded away (unseen).

The tests reference the probe module only through `command.view_probe`, which does
not exist before the fix, so they fail in their assertions there rather than at import.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness.dashboard import command

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux PTY, abstract sockets and /proc are required"
)

# Bound on every wait for a child; the whole module stays far below the CI test timeout.
_DEADLINE_SECONDS = 20.0
_CURSOR_REQUEST = b"\x1b[6n"
_CURSOR_REPLY = b"\x1b[1;1R"

_DASHBOARD_CHILD = """
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from booley.harness.dashboard import app, command
from booley.harness.dashboard.model import DashboardSnapshot

root, scope = Path(sys.argv[1]), sys.argv[2]
# A short heartbeat keeps an answering terminal's age far below the test thresholds.
app.REFRESH_SECONDS = 0.2
command.namespace = lambda: scope
command.resolve_common_booley_dir = lambda _: root
command.DashboardReader = lambda *_: SimpleNamespace(read=DashboardSnapshot)
raise SystemExit(command.run_dashboard(root))
"""

# A stand-in owner that holds the Sandbox socket, never accepts, and ignores SIGTERM.
_WEDGED_CHILD = """
import signal
import socket
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
owner.bind("\\0booley-dashboard:" + sys.argv[1])
owner.listen()
print("ready", flush=True)
time.sleep(60)
"""


def _child_env() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SPT_", "TEXTUAL_")) and key not in {"NO_COLOR", "FORCE_COLOR"}
    }
    source = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    environment["TERM"] = "xterm-256color"
    return environment


def _socket_is_bound(scope: str) -> bool:
    address = "@booley-dashboard:" + scope
    rows = Path("/proc/net/unix").read_text(encoding="utf-8").splitlines()
    return any(row.split()[-1:] == [address] for row in rows)


def _wait_until(condition, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


class _PtyDashboard:
    """A Dashboard child on a PTY whose terminal side answers heartbeats or stays silent."""

    def __init__(self, scope: str, tmp_path: Path, *, answers: bool) -> None:
        # POSIX-only modules, imported here so the module still collects on Windows.
        import fcntl
        import pty
        import termios

        self.scope = scope
        self._answers = answers
        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        try:
            # Kept on subprocess.Popen: PTY child the test drives and signals.
            self.process = subprocess.Popen(
                [sys.executable, "-c", _DASHBOARD_CHILD, str(tmp_path), scope],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=_child_env(),
            )
        finally:
            os.close(slave)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        _wait_until(lambda: _socket_is_bound(scope), "the Dashboard to hold the Sandbox socket")

    def _drain(self) -> None:
        """Read the terminal output like an emulator; answer heartbeats if this one is attached."""
        while True:
            try:
                if not select.select([self._master], [], [], 0.1)[0]:
                    continue
                chunk = os.read(self._master, 65536)
            except OSError:
                return
            if not chunk:
                return
            if self._answers:
                try:
                    for _ in range(chunk.count(_CURSOR_REQUEST)):
                        os.write(self._master, _CURSOR_REPLY)
                except OSError:
                    return

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        # Kept on subprocess.Popen: reaping the PTY child the test signalled.
        self.process.wait(timeout=5)
        self._reader.join(timeout=5)
        os.close(self._master)


@pytest.fixture
def scope(tmp_path):
    return f"pid:[view-owner-test-{os.getpid()}-{tmp_path.name}]"


@pytest.fixture
def newcomer(tmp_path, monkeypatch, scope):
    """An in-process `booley dashboard` for *scope*; `launches` records each Dashboard it starts."""
    launches: list[str] = []
    monkeypatch.setattr(command, "namespace", lambda: scope)
    monkeypatch.setattr(command, "resolve_common_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(command, "attach_blocker", lambda *_: "")
    monkeypatch.setattr(command, "DashboardReader", lambda *_: SimpleNamespace(read=lambda: None))
    monkeypatch.setattr(
        command,
        "DashboardApp",
        lambda *_, **__: SimpleNamespace(run=lambda: launches.append("run")),
    )
    monkeypatch.setattr(command, "_label_terminal", lambda: lambda: None)
    return SimpleNamespace(launches=launches)


def _thresholds(monkeypatch, *, seen: float, unseen: float, reply: float = 0.5) -> None:
    """Shrink the liveness thresholds so tests stay fast; a no-op before the probe module exists."""
    probe = getattr(command, "view_probe", None)
    if probe is None:
        return
    monkeypatch.setattr(probe, "SEEN_WITHIN", seen)
    monkeypatch.setattr(probe, "UNSEEN_AFTER", unseen)
    monkeypatch.setattr(probe, "REPLY_SECONDS", reply)
    monkeypatch.setattr(probe, "TERM_GRACE_SECONDS", 0.5)


def test_unseen_dashboard_is_not_open_and_a_new_one_starts(tmp_path, monkeypatch, scope, newcomer):
    _thresholds(monkeypatch, seen=0.5, unseen=1.5)
    old = _PtyDashboard(scope, tmp_path, answers=False)
    try:
        time.sleep(2.0)  # past the unseen threshold: nobody answered since the start
        policy = command.attach_policy(tmp_path)
        assert policy["already_open"] is False, "an unseen Dashboard counted as open"
        assert policy["enabled"] is True
        exit_code = command.run_dashboard(tmp_path, on_attach=True)
        assert exit_code == 0, f"the new Dashboard did not start (exit {exit_code})"
        assert len(newcomer.launches) == 1
    finally:
        old.close()


def test_takeover_leaves_no_earlier_dashboard_running(tmp_path, monkeypatch, scope, newcomer):
    _thresholds(monkeypatch, seen=0.5, unseen=1.5)
    old = _PtyDashboard(scope, tmp_path, answers=False)
    running_at_launch: list[bool] = []
    original_app = command.DashboardApp

    def watching_app(*args, **kwargs):
        running_at_launch.append(old.process.poll() is None)
        return original_app(*args, **kwargs)

    monkeypatch.setattr(command, "DashboardApp", watching_app)
    try:
        time.sleep(2.0)
        assert command.run_dashboard(tmp_path) == 0
        assert running_at_launch == [False], "no new Dashboard started over the earlier one"
        assert old.process.poll() is not None
    finally:
        old.close()


def test_wedged_owner_is_stopped_before_takeover(tmp_path, monkeypatch, scope, newcomer):
    _thresholds(monkeypatch, seen=0.5, unseen=1.5)
    # Kept on subprocess.Popen: wedged child the test drives and signals.
    wedged = subprocess.Popen(
        [sys.executable, "-c", _WEDGED_CHILD, scope],
        stdout=subprocess.PIPE,
        env=_child_env(),
        text=True,
    )
    try:
        assert wedged.stdout is not None
        assert wedged.stdout.readline().strip() == "ready"
        exit_code = command.run_dashboard(tmp_path, on_attach=True)
        assert exit_code == 0, f"a wedged owner kept the view (exit {exit_code})"
        assert len(newcomer.launches) == 1
        assert wedged.poll() == -signal.SIGKILL, "the wedged owner was not killed"
    finally:
        wedged.kill()
        # Kept on subprocess.Popen: reaping the wedged child the test killed.
        wedged.wait(timeout=5)
        if wedged.stdout is not None:
            wedged.stdout.close()


def test_seen_dashboard_still_refuses(tmp_path, monkeypatch, scope, newcomer, capsys):
    _thresholds(monkeypatch, seen=2.0, unseen=4.0)
    visible = _PtyDashboard(scope, tmp_path, answers=True)
    try:
        time.sleep(4.5)  # beyond the unseen threshold: only heartbeat answers keep it seen
        policy = command.attach_policy(tmp_path)
        assert policy["already_open"] is True
        assert policy["reason"] == "The Dashboard is already open in this Sandbox."
        assert command.run_dashboard(tmp_path, on_attach=True) == command.ALREADY_OPEN
        assert command.run_dashboard(tmp_path) == 0
        assert (
            capsys.readouterr().out.count("Booley Dashboard is already open in this Sandbox") == 2
        )
        assert newcomer.launches == []
        assert visible.process.poll() is None, "a visible Dashboard was stopped"
    finally:
        visible.close()


def test_starting_dashboard_is_not_retired(tmp_path, scope, newcomer, capsys):
    starting = _PtyDashboard(scope, tmp_path, answers=False)
    try:
        assert command.attach_policy(tmp_path)["already_open"] is True
        assert command.run_dashboard(tmp_path, on_attach=True) == command.ALREADY_OPEN
        assert "already open" in capsys.readouterr().out
        assert newcomer.launches == []
        assert starting.process.poll() is None, "a starting Dashboard was stopped"
    finally:
        starting.close()


def test_owner_that_cannot_be_proven_unseen_keeps_the_view(tmp_path, scope, newcomer):
    """Bound but never listening (an older Booley) is refused to, not taken over."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as legacy:
        legacy.bind("\0booley-dashboard:" + scope)
        assert command.attach_policy(tmp_path)["already_open"] is True
        assert command.run_dashboard(tmp_path, on_attach=True) == command.ALREADY_OPEN
    assert newcomer.launches == []
