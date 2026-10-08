"""Deployment catalog, Sandbox guard and repeat-attach policy with cheap fixtures."""

from __future__ import annotations

import errno
from types import SimpleNamespace

import pytest

from booley.harness import booley
from booley.harness.dashboard import command
from booley.runtime import runtime_context


@pytest.mark.parametrize("preview", ["", "0", "true", "1"])
def test_command_catalog_help_location_and_binding(monkeypatch, capsys, preview):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", preview)
    parser = booley._build_parser()
    assert ("dashboard" in booley.command_locations()) == (preview == "1")
    assert ("dashboard" in parser.format_help()) == (preview == "1")
    if preview == "1":
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert booley.command_project_bindings()["dashboard"] is booley.ProjectBinding.REQUIRED
        assert booley.command_locations()["dashboard"] is booley.CommandLocation.SESSION_RUNTIME
        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        with pytest.raises(SystemExit) as raised:
            booley._enforce_runtime_location("dashboard")
        assert raised.value.code == 2
        assert "Sandbox" in capsys.readouterr().err
        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
        booley._enforce_runtime_location("dashboard")
    else:
        with pytest.raises(SystemExit):
            parser.parse_args(["dashboard"])


def test_repeat_attach_one_view_and_no_filesystem_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "namespace", lambda: "pid:[fixture]")
    monkeypatch.setattr(command, "resolve_project_dir", lambda _: tmp_path)
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
    assert launched == ["run"]
    assert "already open" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
