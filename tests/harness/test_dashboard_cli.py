"""Deployment catalog, Sandbox guard and repeat-attach policy with cheap fixtures."""

from __future__ import annotations

import errno
from types import SimpleNamespace

import pytest

from booley.harness import booley
from booley.harness.dashboard import command
from booley.runtime import runtime_context


def test_repeat_attach_one_view_and_no_filesystem_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command.socket, "AF_UNIX", 1, raising=False)
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
