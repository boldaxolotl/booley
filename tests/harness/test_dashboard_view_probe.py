"""Unit coverage for the Dashboard view probe: verdict rules, retirement and heartbeat wiring."""

from __future__ import annotations

import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness.dashboard import command, view_probe
from booley.harness.dashboard.app import CURSOR_POSITION_REQUEST, DashboardApp
from booley.harness.dashboard.model import DashboardSnapshot

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux abstract sockets and /proc are required"
)


@pytest.fixture
def scope(tmp_path):
    return f"pid:[view-probe-test-{os.getpid()}-{tmp_path.name}]"


@pytest.fixture(autouse=True)
def _fast_probe(monkeypatch):
    monkeypatch.setattr(view_probe, "SEEN_WITHIN", 2.0)
    monkeypatch.setattr(view_probe, "UNSEEN_AFTER", 10.0)
    monkeypatch.setattr(view_probe, "REPLY_SECONDS", 0.5)
    monkeypatch.setattr(view_probe, "TERM_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(view_probe, "KILL_GRACE_SECONDS", 0.5)


class _Owner:
    """A Dashboard stand-in in this process: answers each probe with the next scripted reply."""

    def __init__(self, scope, replies):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(view_probe.socket_name(scope))
        self.listener.listen()
        self.replies = list(replies)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        for reply in self.replies:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                if reply is not None:
                    with suppress(OSError):  # the probe may hang up before it reads
                        connection.sendall(reply)
                else:  # accepted, then silent until the probe gives up and hangs up
                    connection.settimeout(5.0)
                    with suppress(OSError):
                        connection.recv(1)

    def close(self):
        self.listener.close()
        self.thread.join(timeout=3)


@pytest.fixture
def owner_for(scope):
    owners = []

    def start(*replies):
        owners.append(_Owner(scope, replies))
        return owners[-1]

    yield start
    for owner in owners:
        owner.close()


def _self_owner():
    return view_probe.ViewOwner(os.getpid(), view_probe._process_stat(os.getpid())[1])


def test_view_state_ages_from_start_until_the_terminal_answers():
    state = view_probe.ViewState()
    time.sleep(0.05)
    assert state.age() >= 0.05
    state.answered()
    assert state.age() < 0.05


def test_serve_answers_with_the_age_and_stops_on_request(scope):
    state = view_probe.ViewState()
    stop = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(view_probe.socket_name(scope))
        listener.listen()
        server = view_probe.serve(listener, state, stop)
        reading = view_probe._read_owner(scope)
        assert reading is not None
        assert reading.owner == _self_owner()
        assert reading.age is not None
        assert 0 <= reading.age < 5
        # A peer that vanishes before the reply must not end the server.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rude:
            rude.connect(view_probe.socket_name(scope))
        assert view_probe._read_owner(scope) is not None
        stop.set()
        server.join(timeout=3)
        assert not server.is_alive()


def test_serve_ends_when_its_socket_is_closed(scope):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(view_probe.socket_name(scope))
    listener.listen()
    server = view_probe.serve(listener, view_probe.ViewState(), threading.Event())
    listener.close()
    server.join(timeout=3)
    assert not server.is_alive()


def test_process_stat_reads_state_and_start_time_past_odd_command_names(monkeypatch):
    own = view_probe._process_stat(os.getpid())
    assert own is not None
    assert own[0] in "RSD"
    assert view_probe._process_stat(2**22 + 12345) is None
    fields = " ".join(["S"] + ["0"] * 18 + ["4242"] + ["0"] * 5)
    monkeypatch.setattr(Path, "read_text", lambda *_, **__: f"1 (a b) c) {fields}\n")
    assert view_probe._process_stat(1) == ("S", "4242")
    monkeypatch.setattr(Path, "read_text", lambda *_, **__: "1 (short) S 1 2\n")
    assert view_probe._process_stat(1) is None


def test_nothing_listening_is_not_provably_unseen(scope):
    assert view_probe.unseen_owner(scope) is None  # no such socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound_only:
        bound_only.bind(view_probe.socket_name(scope))  # an older Booley never listens
        assert view_probe.unseen_owner(scope) is None


@pytest.mark.parametrize(
    ("reply", "unseen"),
    [
        (b"0.500\n", False),
        (b"12.000\n", True),
        (b"garbage\n", False),
        (b"nan\n", False),
        (b"-3\n", False),
        (b"\xff\xfe\n", False),
        (b"", False),
        (None, True),  # accepts the connection, never answers
    ],
)
def test_reply_decides_the_verdict(owner_for, scope, reply, unseen):
    owner_for(reply)
    assert (view_probe.unseen_owner(scope) == _self_owner()) is unseen


def test_one_late_reply_does_not_make_a_dashboard_unseen(owner_for, scope):
    owner_for(None, b"0.200\n")  # too slow for the first look, answers the second
    assert view_probe.unseen_owner(scope) is None


def test_a_silent_owner_is_unseen_after_two_looks(owner_for, scope):
    owner_for(None, None)
    assert view_probe.unseen_owner(scope) == _self_owner()


def test_serve_survives_a_transient_accept_failure():
    sent: list[bytes] = []
    stop = threading.Event()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _seconds):
            pass

        def sendall(self, data):
            sent.append(data)
            stop.set()

    class Listener:
        calls = 0

        def settimeout(self, _seconds):
            pass

        def fileno(self):
            return 7  # still open

        def accept(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError(errno.EMFILE, "no free descriptor")
            return Connection(), None

    server = view_probe.serve(Listener(), view_probe.ViewState(), stop)  # type: ignore[arg-type]
    server.join(timeout=3)
    assert not server.is_alive()
    assert len(sent) == 1, "the reply thread gave up after one failed accept"


def test_other_users_owner_is_not_provably_unseen(owner_for, scope, monkeypatch):
    owner_for(b"99.0\n")
    monkeypatch.setattr(view_probe.os, "getuid", lambda: 2**31 - 2)
    assert view_probe.unseen_owner(scope) is None


@pytest.mark.parametrize(
    ("replies", "gone_meanwhile", "unseen"),
    [
        ([b"3.0\n", b"12.0\n"], False, True),  # silent through the wait
        ([b"3.0\n", b"0.2\n"], False, False),  # answered meanwhile
        ([b"3.0\n"], True, False),  # gone before the second look
        ([b"3.0\n", None], False, True),  # wedged on the second look
    ],
)
def test_undecided_age_is_asked_again_once(
    owner_for, scope, monkeypatch, replies, gone_meanwhile, unseen
):
    waits = []
    owner = owner_for(*replies)

    def wait(seconds):
        waits.append(seconds)
        if gone_meanwhile:
            owner.listener.close()

    monkeypatch.setattr(view_probe.time, "sleep", wait)
    verdict = view_probe.unseen_owner(scope)
    assert (verdict == _self_owner()) if unseen else verdict is None
    assert waits == [pytest.approx(view_probe.UNSEEN_AFTER - 3.0 + view_probe._REPROBE_MARGIN)]


def test_a_different_process_on_the_second_look_is_not_unseen(monkeypatch):
    first = view_probe._Reading(view_probe.ViewOwner(1, "10"), 3.0)
    second = view_probe._Reading(view_probe.ViewOwner(2, "20"), 99.0)
    readings = iter([first, second])
    monkeypatch.setattr(view_probe, "_read_owner", lambda _scope: next(readings))
    monkeypatch.setattr(view_probe.time, "sleep", lambda _: None)
    assert view_probe.unseen_owner("any") is None


def _spawn(code: str) -> subprocess.Popen[str]:
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    return child


def _owner_of(child: subprocess.Popen[str]) -> view_probe.ViewOwner:
    stat = view_probe._process_stat(child.pid)
    assert stat is not None
    return view_probe.ViewOwner(child.pid, stat[1])


def _reap(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
    # Kept on subprocess.Popen: child the test drives and signals.
    child.wait(timeout=5)
    if child.stdout is not None:
        child.stdout.close()


def test_retire_stops_a_dashboard_with_sigterm():
    child = _spawn("import time; print('ready', flush=True); time.sleep(60)")
    try:
        assert view_probe.retire(_owner_of(child)) is True
        assert child.poll() == -signal.SIGTERM
    finally:
        _reap(child)


def test_retire_escalates_to_sigkill_for_a_process_that_ignores_sigterm():
    child = _spawn(
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)"
    )
    try:
        assert view_probe.retire(_owner_of(child)) is True
        assert child.poll() == -signal.SIGKILL
    finally:
        _reap(child)


def test_retire_never_signals_a_reused_pid_or_a_missing_process(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("must not signal"))
    stale = view_probe.ViewOwner(os.getpid(), "1")  # not this process's start time
    assert view_probe.retire(stale) is True
    assert view_probe.retire(view_probe.ViewOwner(2**22 + 12345, "1")) is True


def test_retire_reports_a_process_it_cannot_stop(monkeypatch):
    owner = _self_owner()
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
    assert view_probe.retire(owner) is False  # still there after both grace periods
    assert sent == [signal.SIGTERM, signal.SIGKILL]

    def refuse(*_):
        raise PermissionError

    monkeypatch.setattr(os, "kill", refuse)
    assert view_probe.retire(owner) is False

    def vanished(*_):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", vanished)
    assert view_probe.retire(owner) is True


def test_heartbeat_writes_the_cursor_request_and_records_the_answer():
    state = view_probe.ViewState()
    app = DashboardApp(DashboardSnapshot, view_state=state)
    written = []
    app._driver = SimpleNamespace(write=written.append, flush=lambda: written.append("flush"))
    app._request_view_heartbeat()
    assert written == [CURSOR_POSITION_REQUEST, "flush"]
    time.sleep(0.05)
    app.on_cursor_position(SimpleNamespace())
    assert state.age() < 0.05


def test_dashboard_without_a_view_state_neither_asks_nor_listens():
    app = DashboardApp(DashboardSnapshot)
    app._driver = SimpleNamespace(write=lambda _: pytest.fail("no heartbeat"), flush=lambda: None)
    app._request_view_heartbeat()
    app.on_cursor_position(SimpleNamespace())
    assert DashboardApp(DashboardSnapshot, view_state=view_probe.ViewState())._driver is None


class _Claim:
    """Socket stand-in whose bind attempts follow a script of errnos (None binds)."""

    def __init__(self, *errnos):
        self.errnos = list(errnos)
        self.binds = 0

    def bind(self, _address):
        self.binds += 1
        code = self.errnos.pop(0)
        if code is not None:
            raise OSError(code, "scripted")


def test_claiming_the_view_covers_every_outcome(monkeypatch, capsys):
    import errno

    retired = []
    monkeypatch.setattr(command.view_probe, "retire", lambda owner: retired.append(owner) or True)
    stale = view_probe.ViewOwner(7, "70")
    monkeypatch.setattr(command.view_probe, "unseen_owner", lambda _scope: stale)

    claim = _Claim(None)
    assert command._bind_view(claim, "s", on_attach=True) is None

    claim = _Claim(errno.EADDRINUSE, None)
    assert command._bind_view(claim, "s", on_attach=True) is None
    assert retired == [stale]

    # The retired process's address frees a moment after it shows as gone.
    claim = _Claim(errno.EADDRINUSE, errno.EADDRINUSE, errno.EADDRINUSE, None)
    assert command._bind_view(claim, "s", on_attach=True) is None
    assert claim.binds == 4

    # Another newcomer won the race for the freed address.
    monkeypatch.setattr(command.view_probe, "RELEASE_GRACE_SECONDS", 0.2)
    claim = _Claim(*[errno.EADDRINUSE] * 100)
    assert command._bind_view(claim, "s", on_attach=True) == command.ALREADY_OPEN
    assert command._bind_view(_Claim(*[errno.EADDRINUSE] * 100), "s", on_attach=False) == 0
    assert "already open" in capsys.readouterr().out

    assert command._bind_view(_Claim(errno.EACCES), "s", on_attach=False) == 2
    assert "cannot reserve" in capsys.readouterr().out

    monkeypatch.setattr(command.view_probe, "retire", lambda _owner: False)
    assert command._bind_view(_Claim(errno.EADDRINUSE), "s", on_attach=False) == 2
    assert "unresponsive Dashboard" in capsys.readouterr().out

    monkeypatch.setattr(command.view_probe, "unseen_owner", lambda _scope: None)
    assert (
        command._bind_view(_Claim(errno.EADDRINUSE), "s", on_attach=True) == command.ALREADY_OPEN
    )


def test_attach_reports_unreadable_project_policy(tmp_path, monkeypatch, capsys):
    def broken(*_):
        raise ValueError("invalid dashboard configuration")

    monkeypatch.setattr(command, "attach_blocker", broken)
    monkeypatch.setattr(command, "resolve_common_booley_dir", lambda _: tmp_path)
    assert command.run_attach(tmp_path) == 2
    assert "invalid dashboard configuration" in capsys.readouterr().err
