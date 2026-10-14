"""Help flags cannot admit Flow or Specialist execution from the host."""

from __future__ import annotations

import argparse
import subprocess
from contextlib import contextmanager

import pytest

from booley.harness import booley as cli
from booley.mcp.base import McpTool
from booley.mcp.registry import McpToolInfo


@pytest.fixture
def project(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".booley").mkdir()
    monkeypatch.chdir(tmp_path)
    for name in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(cli, "_invoke_endpoint", lambda *_a, **_k: pytest.fail("execution"))
    monkeypatch.setattr(
        cli.runtime_context, "ensure_proxy_env", lambda: pytest.fail("proxy restoration")
    )
    return tmp_path


@pytest.mark.parametrize("command,name", [("flow", "lint"), ("specialist", "reviewer")])
@pytest.mark.parametrize("tail", [[], ["--target", "demo"]])
def test_host_refusal_precedes_execution_admission(
    project, command, name, tail, monkeypatch, capsys
):
    import booley.runtime.layout_consumers as consumers

    monkeypatch.setattr(
        consumers, "directory_consumer", lambda *_a: pytest.fail("execution admission")
    )
    route = [command] if not tail else [command, name, *tail]
    monkeypatch.setattr("sys.argv", ["booley", *route])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


class Custom(McpTool):
    name = "custom"
    description = "Custom Flow"

    def _add_args(self, parser):
        parser.add_argument("--number", type=int)
        parser.add_argument("payload", nargs="*")

    def _run(self):
        pytest.fail("custom execution")


def register_custom(monkeypatch, endpoint_type):
    info = McpToolInfo(name="custom", path="custom.py", description="Custom", kind="flow")
    monkeypatch.setattr(cli, "_discover_project_mcp_tools", lambda _root: [info])
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: endpoint_type)


def test_literal_help_after_separator_cannot_bypass_host_guard(project, monkeypatch, capsys):
    register_custom(monkeypatch, Custom)
    # Argparse and the dispatcher each consume one leading separator.
    # The final separator belongs to the custom parser's literal payload.
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--", "--", "--", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runs inside the Booley Sandbox" in captured.err


def test_parser_error_is_reported_once(project, monkeypatch, capsys):
    register_custom(monkeypatch, Custom)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--number", "bad", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("usage:") == 1
    assert "invalid int value" in captured.err
    assert "runs inside" not in captured.err


def test_custom_exit_zero_is_not_help(project, monkeypatch, capsys):
    calls = []

    class ExitZero(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            calls.append("version")
            print("custom version")
            parser.exit()

    class Versioned(Custom):
        def _add_args(self, parser):
            super()._add_args(parser)
            parser.add_argument("--version", action=ExitZero, nargs=0)

    register_custom(monkeypatch, Versioned)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--version", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runs inside the Booley Sandbox" in captured.err
    assert calls == ["version"]


def test_named_help_retains_metadata_admission(project, monkeypatch, capsys):
    import booley.runtime.layout_consumers as consumers

    events = []

    @contextmanager
    def lease(root):
        assert root == project
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr(consumers, "directory_consumer", lease)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "lint", "--target", "demo", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "usage: booley flow lint" in capsys.readouterr().out
    assert events == ["admitted", "released"]


def test_override_cannot_run_through_host_help(project, monkeypatch, capsys):
    class Override(Custom):
        def main(self, argv=None):
            pytest.fail("overridden main")

    register_custom(monkeypatch, Override)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--help"])
    assert cli.main() == 2
    assert "shared CLI" in capsys.readouterr().err


def test_combined_quiet_help_preserves_flow_options(project, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["booley", "flow", "lint", "-qh"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "--quiet" in capsys.readouterr().out


def test_help_named_option_is_not_a_help_action(project, monkeypatch, capsys):
    class HelpLike(Custom):
        @property
        def _parser(self):
            if self._cli_parser is None:
                self._cli_parser = argparse.ArgumentParser(add_help=False)
                self._add_common_args()
                self._add_args(self._cli_parser)
            return self._cli_parser

        def _add_args(self, parser):
            super()._add_args(parser)
            parser.add_argument("--help", action="store_true")

    register_custom(monkeypatch, HelpLike)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


def test_help_looking_data_is_parsed_once_in_sandbox(project, monkeypatch):
    calls = []

    class Hint(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            calls.append("hint")

    class Hinted(Custom):
        def _add_args(self, parser):
            super()._add_args(parser)
            parser.add_argument("--hint", action=Hint, nargs=0)

    def invoke(endpoint, argv, args):
        endpoint.parse_args(argv)
        return 0

    register_custom(monkeypatch, Hinted)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)
    monkeypatch.setattr(cli, "_invoke_endpoint", invoke)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--hint"])
    assert cli.main() == 0
    assert calls == ["hint"]


def test_missing_project_error_precedes_host_venue_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr("sys.argv", ["booley", "flow"])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert "Project" in err
    assert "runs inside the Booley Sandbox" not in err
