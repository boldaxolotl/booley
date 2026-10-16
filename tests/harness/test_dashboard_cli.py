"""Deployment catalog, Sandbox guard and repeat-attach policy with cheap fixtures."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness import booley
from booley.harness.dashboard import command
from booley.runtime import runtime_context


@pytest.fixture(autouse=True)
def _keep_pytest_process_identity(monkeypatch):
    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=lambda _: None))


def test_repeat_attach_one_view_and_no_filesystem_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(command, "namespace", lambda: "pid:[fixture]")
    monkeypatch.setattr(command, "resolve_booley_dir", lambda _: tmp_path)
    launched = []
    monkeypatch.setattr(command, "DashboardReader", lambda *_: SimpleNamespace(read=lambda: None))
    monkeypatch.setattr(
        command, "DashboardApp", lambda *_: SimpleNamespace(run=lambda: launched.append("run"))
    )

    class Owner:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def bind(self, address):
            assert address.startswith("\0booley-dashboard:")
            if launched:
                raise OSError(errno.EADDRINUSE, "in use")

    monkeypatch.setattr(command.socket, "socket", lambda *_: Owner())
    assert command.run_dashboard(tmp_path) == 0
    assert command.run_dashboard(tmp_path) == 0
    assert command.run_dashboard(tmp_path, on_attach=True) == command.ALREADY_OPEN
    assert launched == ["run"]
    assert "already open" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_attach_policy_is_read_only_and_disabled_attach_never_starts(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(command, "resolve_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(command, "attach_blocker", lambda *_: "disabled by configuration")
    monkeypatch.setattr(command, "run_dashboard", lambda *_: pytest.fail("must not start"))
    assert command.run_attach(tmp_path, policy_only=True) == 0
    assert json.loads(capsys.readouterr().out) == {
        "protocol": 1,
        "enabled": False,
        "reason": "disabled by configuration",
        "already_open": False,
    }
    assert command.run_attach(tmp_path) == command.ATTACH_DISABLED
    assert list(tmp_path.iterdir()) == []


def test_attach_rechecks_policy_before_launch_and_surfaces_bad_configuration(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(command, "resolve_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(command, "attach_blocker", lambda *_: "")
    monkeypatch.setattr(command, "_dashboard_is_open", lambda: False)
    launches = []
    monkeypatch.setattr(
        command, "run_dashboard", lambda root, **kw: launches.append((root, kw)) or 0
    )
    assert command.run_attach(tmp_path) == 0
    assert launches == [(tmp_path, {"on_attach": True})]

    def invalid(*_):
        raise ValueError("invalid dashboard configuration")

    monkeypatch.setattr(command, "attach_blocker", invalid)
    assert command.run_attach(tmp_path, policy_only=True) == 2
    assert "invalid dashboard configuration" in capsys.readouterr().err


def test_dashboard_without_unix_sockets_is_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.delattr(command.socket, "AF_UNIX", raising=False)
    monkeypatch.setattr(command, "namespace", lambda: "fixture")
    assert command.run_dashboard(tmp_path) == 2
    assert "AF_UNIX" in capsys.readouterr().out


def test_dashboard_catalog_help_location_and_binding(monkeypatch, capsys):
    parser = booley._build_parser()
    assert "dashboard" in parser.format_help()
    assert parser.parse_args(["dashboard"]).command == "dashboard"
    assert booley.COMMAND_PROJECT_BINDINGS["dashboard"] is booley.ProjectBinding.REQUIRED
    assert booley.COMMAND_LOCATIONS["dashboard"] is booley.CommandLocation.SESSION_RUNTIME
    assert "dashboard" in booley._CONTAINER_ONLY_COMMANDS
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    with pytest.raises(SystemExit) as raised:
        booley._enforce_runtime_location("dashboard")
    assert raised.value.code == 2
    assert "Sandbox" in capsys.readouterr().err
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    booley._enforce_runtime_location("dashboard")


def test_goal_and_dashboard_configuration_is_always_validated():
    from booley.audit import project_schema

    data = {"goals": {"quiet_after": "invalid"}, "sandbox": {"dashboard": "invalid"}}
    assert not project_schema.audit_known_tables(data).findings
    assert not project_schema.audit_sandbox_table(data).is_valid
    assert not project_schema.audit_goals_table(data).is_valid


def test_dashboard_preflight_observes_without_reserving_a_socket(monkeypatch):
    if not Path("/proc/net/unix").exists() or not hasattr(socket, "AF_UNIX"):
        pytest.skip("Linux abstract socket inventory is required")
    scope = f"pid:[attach-policy-test-{os.getpid()}]"
    monkeypatch.setattr(command, "namespace", lambda: scope)
    assert not command._dashboard_is_open()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as owner:
        owner.bind("\0booley-dashboard:" + scope)
        with monkeypatch.context() as probe:
            probe.setattr(command.socket, "socket", lambda *_: pytest.fail("probe must not bind"))
            assert command._dashboard_is_open()
    assert not command._dashboard_is_open()


_TITLE_PUSH = "\x1b[22;2t"
_TITLE_SET = "\x1b]2;Booley Dashboard\x07"
_TITLE_POP = "\x1b[23;2t"


class _TitleStream:
    """Record writes independently of flush, including partial acquisition failures."""

    def __init__(self, *, tty=True, fail_write="", fail_flush=False):
        self.tty = tty
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.writes = []
        self.flushes = 0

    def isatty(self):
        if isinstance(self.tty, BaseException):
            raise self.tty
        return self.tty

    def write(self, text):
        if text == self.fail_write:
            raise OSError("unavailable stream")
        self.writes.append(text)
        return len(text)

    def flush(self):
        self.flushes += 1
        if self.fail_flush:
            raise ValueError("closed stream")


def _label_helper():
    helper = getattr(command, "_label_terminal", None)
    assert callable(helper), "Dashboard terminal label helper is missing"
    return helper


def _stub_dashboard(monkeypatch, tmp_path, *, run=None):
    """Keep in-process tests away from process naming and real socket ownership."""
    import sys

    events = []
    owner = SimpleNamespace(bind=lambda _: None)

    class Socket:
        def __enter__(self):
            return owner

        def __exit__(self, *_):
            events.append("socket closed")

    monkeypatch.setattr(command.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(command.socket, "socket", lambda *_: Socket())
    monkeypatch.setattr(command, "namespace", lambda: "pid:[title-fixture]")
    monkeypatch.setattr(command, "resolve_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(command, "DashboardReader", lambda *_: SimpleNamespace(read=lambda: None))
    monkeypatch.setattr(
        command,
        "DashboardApp",
        lambda *_: SimpleNamespace(run=run or (lambda: events.append("run"))),
    )
    monkeypatch.setattr(command, "_PLATFORM", "linux", raising=False)
    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=events.append))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return events, owner


def _dashboard_child_env():
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SPT_", "TEXTUAL_")) and key not in {"NO_COLOR", "FORCE_COLOR"}
    }
    source = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    environment["TERM"] = "xterm-256color"
    return environment


def test_dashboard_label_sets_linux_proc_identity():
    import subprocess
    import sys

    if sys.platform != "linux":
        pytest.skip("Linux /proc process identity is required")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from pathlib import Path; "
            "from booley.harness.dashboard import command; "
            "helper = getattr(command, '_label_terminal', None); "
            "restore = helper() if callable(helper) else None; "
            "print(json.dumps({'available': callable(helper), "
            "'comm': Path('/proc/self/comm').read_text().strip(), "
            "'cmdline': Path('/proc/self/cmdline').read_bytes().split(bytes([0]))[0].decode()})); "
            "restore() if restore else None",
        ],
        env=_dashboard_child_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    identity = json.loads(child.stdout)
    assert identity["available"], "Dashboard terminal label helper is missing"
    assert identity["comm"] == "dashboard"
    assert identity["cmdline"] == "dashboard"
    assert child.stderr == ""


def test_dashboard_restores_title_on_all_exit_paths(tmp_path, monkeypatch):
    import sys

    for failure in (None, RuntimeError("app failure"), KeyboardInterrupt()):
        with monkeypatch.context() as patch:
            stream = _TitleStream()

            events, _ = _stub_dashboard(patch, tmp_path)

            def run(failure=failure, stream=stream, events=events):
                assert stream.writes == [_TITLE_PUSH, _TITLE_SET], (
                    "Dashboard startup title is missing"
                )
                assert stream.flushes > 0
                assert events == ["dashboard"]
                if failure is not None:
                    raise failure

            patch.setattr(command, "DashboardApp", lambda *_: SimpleNamespace(run=run))
            patch.setattr(sys, "stdout", stream)
            if failure is None:
                assert command.run_dashboard(tmp_path) == 0
            else:
                with pytest.raises(type(failure)) as raised:
                    command.run_dashboard(tmp_path)
                assert raised.value is failure
            assert stream.writes == [_TITLE_PUSH, _TITLE_SET, _TITLE_POP]
            assert events[-1] == "socket closed"


_PTY_DASHBOARD = """
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from booley.harness.dashboard import command
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.model import DashboardSnapshot

class FirstRefreshDashboard(DashboardApp):
    async def on_mount(self):
        await super().on_mount()
        self.call_after_refresh(self.exit)

command.namespace = lambda: f"pid:[terminal-title-test-{os.getpid()}]"
command.resolve_booley_dir = lambda _: Path(sys.argv[1])
command.DashboardReader = lambda *_: SimpleNamespace(read=DashboardSnapshot)
command.DashboardApp = FirstRefreshDashboard
raise SystemExit(command.run_dashboard(Path(sys.argv[1])))
"""


def _drain_dashboard_pty(child, master):
    import errno
    import select
    import time

    captured = bytearray()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not select.select([master], [], [], 0.05)[0]:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        captured.extend(chunk)
        assert len(captured) <= 1024 * 1024, "Dashboard PTY output exceeded 1 MiB"
    else:
        pytest.fail("Dashboard PTY child exceeded its 10-second deadline")
    return child.wait(timeout=max(0.1, deadline - time.monotonic())), bytes(captured)


def _run_dashboard_pty(tmp_path):
    import fcntl
    import pty
    import struct
    import subprocess
    import sys
    import termios

    master, slave = pty.openpty()
    child = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        child = subprocess.Popen(
            [sys.executable, "-c", _PTY_DASHBOARD, str(tmp_path)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=_dashboard_child_env(),
        )
        os.close(slave)
        slave = None
        return _drain_dashboard_pty(child, master)
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        os.close(master)
        if slave is not None:
            os.close(slave)


def test_dashboard_textual_preserves_terminal_title(tmp_path):
    import re
    import sys

    if sys.platform != "linux":
        pytest.skip("Linux PTY and abstract sockets are required")
    status, output = _run_dashboard_pty(tmp_path)
    assert status == 0, output.decode(errors="replace")
    assert b"\x1b[?1049h" in output, "Textual alternate screen did not start"
    assert b"\x1b[?1049l" in output, "Textual alternate screen did not stop"
    assert b"already open" not in output
    assert _TITLE_PUSH.encode() in output, "Dashboard startup title is missing"
    push, title, pop = (_TITLE_PUSH.encode(), _TITLE_SET.encode(), _TITLE_POP.encode())
    assert output.count(push) == output.count(pop) == 1
    assert output.index(push) < output.index(title) < output.index(b"\x1b[?1049h")
    assert output.index(b"\x1b[?1049l") < output.index(pop)
    assert re.findall(rb"\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)", output) == [title]


def test_dashboard_label_plain_output_is_silent(tmp_path, monkeypatch):
    import sys

    helper = _label_helper()
    scenarios = [
        (False, "xterm", None, "1"),
        (True, "dumb", None, "1"),
        (True, "xterm", "1", "1"),
        (OSError("no tty"), "xterm", None, None),
        (ValueError("closed"), "xterm", None, None),
        (None, "xterm", None, None),
    ]
    for tty, term, no_color, force_color in scenarios:
        with monkeypatch.context() as patch:
            events, _ = _stub_dashboard(patch, tmp_path)
            stream = _TitleStream(tty=tty)
            if tty is None:
                stream = SimpleNamespace(
                    writes=stream.writes, write=stream.write, flush=stream.flush
                )
            patch.setattr(sys, "stdout", stream)
            patch.setenv("TERM", term)
            for key, value in (("NO_COLOR", no_color), ("FORCE_COLOR", force_color)):
                patch.delenv(key, raising=False) if value is None else patch.setenv(key, value)
            assert command.run_dashboard(tmp_path) == 0
            assert events == ["dashboard", "run", "socket closed"]
            assert getattr(stream, "writes", []) == []
    with monkeypatch.context() as patch:
        _stub_dashboard(patch, tmp_path)
        stream = _TitleStream()
        patch.setattr(sys, "stdout", stream)
        patch.delenv("TERM")
        patch.setenv("NO_COLOR", "")
        restore = helper()
        restore()
        restore()
        assert stream.writes == [_TITLE_PUSH, _TITLE_SET, _TITLE_POP]


def _unsupported_label_case(patch, case, events, stream):
    import builtins
    import sys

    if case == "import":
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "setproctitle":
                raise ImportError("not installed")
            return original(name, *args, **kwargs)

        patch.setattr(builtins, "__import__", unavailable)
    elif case == "platform":
        patch.setattr(command, "_PLATFORM", "win32")
    elif case == "setter":

        def unavailable(_):
            raise RuntimeError("not supported")

        patch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=unavailable))
    elif case == "stream":
        stream.tty = AttributeError("no tty method")
    events.clear()


def test_dashboard_label_unsupported_is_silent(tmp_path, monkeypatch, caplog, capsys):
    import sys

    _label_helper()
    for case in ("import", "platform", "setter", "stream", "push", "title", "flush", "pop"):
        with monkeypatch.context() as patch:
            events, _ = _stub_dashboard(patch, tmp_path)
            stream = _TitleStream(
                fail_write={"push": _TITLE_PUSH, "title": _TITLE_SET, "pop": _TITLE_POP}.get(
                    case, ""
                ),
                fail_flush=case == "flush",
            )
            _unsupported_label_case(patch, case, events, stream)
            patch.setattr(sys, "stdout", stream)
            assert command.run_dashboard(tmp_path) == 0
            assert "run" in events
            assert stream.writes == (
                []
                if case in {"platform", "stream", "push"}
                else (
                    [_TITLE_PUSH, _TITLE_POP]
                    if case == "title"
                    else [_TITLE_PUSH, _TITLE_SET]
                    if case == "pop"
                    else [_TITLE_PUSH, _TITLE_SET, _TITLE_POP]
                )
            )
            assert ("dashboard" in events) == (case not in {"import", "platform", "setter"})
    assert caplog.records == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_dashboard_restores_title_if_app_construction_fails(tmp_path, monkeypatch):
    import sys

    _stub_dashboard(monkeypatch, tmp_path)
    stream = _TitleStream(fail_write=_TITLE_POP)
    monkeypatch.setattr(sys, "stdout", stream)
    failure = RuntimeError("construction failed")

    def fail(*_):
        raise failure

    monkeypatch.setattr(command, "DashboardApp", fail)
    with pytest.raises(RuntimeError) as raised:
        command.run_dashboard(tmp_path)
    assert raised.value is failure
    assert stream.writes == [_TITLE_PUSH, _TITLE_SET]


def test_dashboard_label_restores_original_stream_on_interrupted_acquisition(monkeypatch):
    import sys

    helper = _label_helper()
    stream = _TitleStream()
    original_write = stream.write

    def interrupted(text):
        if text == _TITLE_SET:
            raise KeyboardInterrupt()
        return original_write(text)

    monkeypatch.setattr(command, "_PLATFORM", "linux", raising=False)
    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=lambda _: None))
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", stream)
    stream.write = interrupted
    with pytest.raises(KeyboardInterrupt):
        helper()
    assert stream.writes == [_TITLE_PUSH, _TITLE_POP]
    stream.write = original_write
    restore = helper()
    monkeypatch.setattr(sys, "stdout", _TitleStream())
    restore()
    restore()
    assert stream.writes[-3:] == [_TITLE_PUSH, _TITLE_SET, _TITLE_POP]


def test_dashboard_does_not_label_refusals_or_policy_probes(tmp_path, monkeypatch, capsys):
    _, owner = _stub_dashboard(monkeypatch, tmp_path)
    monkeypatch.setattr(
        command, "_label_terminal", lambda: pytest.fail("must not label"), raising=False
    )
    monkeypatch.setattr(command, "namespace", lambda: None)
    assert command.run_dashboard(tmp_path) == 2
    monkeypatch.setattr(command, "namespace", lambda: "pid:[fixture]")

    def occupied(_):
        raise OSError(errno.EADDRINUSE, "in use")

    owner.bind = occupied
    assert command.run_dashboard(tmp_path) == 0
    assert command.run_dashboard(tmp_path, on_attach=True) == command.ALREADY_OPEN
    monkeypatch.setattr(command, "attach_blocker", lambda *_: "disabled by configuration")
    assert command.run_attach(tmp_path) == command.ATTACH_DISABLED
    assert command.run_attach(tmp_path, policy_only=True) == 0
    assert "disabled by configuration" in capsys.readouterr().out
