"""Help flags cannot admit Flow or Specialist execution from the host."""

from __future__ import annotations

import argparse
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from booley.harness import booley as cli
from booley.mcp.base import McpTool
from booley.mcp.registry import McpToolInfo


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    project: Path,
    command: str,
    name: str,
    tail: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    name: ClassVar[str] = "custom"
    description: ClassVar[str] = "Custom Flow"

    def _add_args(self, parser):
        parser.add_argument("--number", type=int)
        parser.add_argument("payload", nargs="*")

    def _run(self):
        pytest.fail("custom execution")


def register_custom(monkeypatch: pytest.MonkeyPatch, endpoint_type: type[McpTool]) -> None:
    info = McpToolInfo(name="custom", path="custom.py", description="Custom", kind="flow")
    monkeypatch.setattr(cli, "_discover_project_mcp_tools", lambda _root: [info])
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: endpoint_type)


def test_literal_help_after_separator_cannot_bypass_host_guard(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_parser_error_is_reported_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


@pytest.mark.parametrize("exit_code", [0, 2, 7])
def test_custom_exit_is_not_help(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
) -> None:
    calls = []

    class ExitValue(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Any,
            option_string: str | None = None,
        ) -> None:
            calls.append("version")
            print("custom version")
            parser.exit(exit_code)

    class Versioned(Custom):
        def _add_args(self, parser):
            super()._add_args(parser)
            parser.add_argument("--version", action=ExitValue, nargs=0)

    register_custom(monkeypatch, Versioned)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--version", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runs inside the Booley Sandbox" in captured.err
    assert calls == ["version"]


def test_named_help_retains_metadata_admission(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_override_cannot_run_through_host_help(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Override(Custom):
        def main(self, argv: list[str] | None = None) -> int:
            pytest.fail("overridden main")

    register_custom(monkeypatch, Override)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "--help"])
    assert cli.main() == 2
    assert "shared CLI" in capsys.readouterr().err


def test_combined_quiet_help_preserves_flow_options(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["booley", "flow", "lint", "-qh"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "--quiet" in capsys.readouterr().out


def test_help_named_option_is_not_a_help_action(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_help_looking_data_is_parsed_once_in_sandbox(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Hint(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Any,
            option_string: str | None = None,
        ) -> None:
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


def test_missing_project_error_precedes_host_venue_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr("sys.argv", ["booley", "flow"])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert "Project" in err
    assert "runs inside the Booley Sandbox" not in err


class Nested(Custom):
    def _add_args(self, parser):
        commands = parser.add_subparsers(dest="operation")
        nested = commands.add_parser("nested", aliases=["n"])
        nested.add_argument("--number", type=int)


@pytest.mark.parametrize("name", ["nested", "n"])
def test_nested_help_uses_the_dispatched_parser(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    register_custom(monkeypatch, Nested)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", name, "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "nested" in captured.out
    assert "--number" in captured.out
    assert captured.err == ""


def test_nested_syntax_error_is_reported_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_custom(monkeypatch, Nested)
    monkeypatch.setattr(
        "sys.argv", ["booley", "flow", "custom", "nested", "--number", "bad", "--help"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("usage:") == 1
    assert "invalid int value" in captured.err
    assert "runs inside" not in captured.err


def test_nested_custom_action_runs_once_before_help(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []

    class Hint(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Any,
            option_string: str | None = None,
        ) -> None:
            calls.append("hint")

    class Hinted(Nested):
        def _add_args(self, parser):
            super()._add_args(parser)
            commands = next(
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            )
            commands.choices["nested"].add_argument("--hint", action=Hint, nargs=0)

    register_custom(monkeypatch, Hinted)
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", "nested", "--hint", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr().err == ""
    assert calls == ["hint"]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("inside", [False, True])
def test_help_preserves_registered_type_converters(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nested: bool,
    inside: bool,
) -> None:
    from booley.flows.endpoint_cli import human_endpoint_parser

    class Registered(Custom):
        def _add_args(self, parser):
            if nested:
                parser = parser.add_subparsers().add_parser("nested")
            parser.register("type", "hexadecimal", lambda value: int(value, 16))
            parser.add_argument("--number", type="hexadecimal")

    register_custom(monkeypatch, Registered)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    tail = ["nested"] if nested else []
    tail.extend(["--number", "ff", "--help"])
    with pytest.raises(SystemExit) as direct:
        human_endpoint_parser(Registered()).parse_args(tail)
    assert direct.value.code == 0
    expected = capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", *tail])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr() == expected


@pytest.mark.parametrize("command", ["flow", "specialist"])
def test_unknown_endpoint_help_cannot_list_from_host(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setattr("sys.argv", ["booley", command, "unknown-endpoint", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Available" not in captured.err
    assert "runs inside the Booley Sandbox" in captured.err


@pytest.mark.parametrize("nested", [False, True])
def test_help_preserves_titled_mutually_exclusive_groups(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nested: bool,
) -> None:
    from booley.flows.endpoint_cli import human_endpoint_parser

    class Grouped(Custom):
        def _add_args(self, parser):
            if nested:
                parser = parser.add_subparsers().add_parser("nested")
            group = parser.add_argument_group("Custom group")
            exclusive = group.add_mutually_exclusive_group()
            exclusive.add_argument("--one", action="store_true")
            exclusive.add_argument("--two", action="store_true")

    register_custom(monkeypatch, Grouped)
    tail = ["nested", "--help"] if nested else ["--help"]
    with pytest.raises(SystemExit) as direct:
        human_endpoint_parser(Grouped()).parse_args(tail)
    assert direct.value.code == 0
    expected = capsys.readouterr()
    assert "Custom group:" in expected.out
    monkeypatch.setattr("sys.argv", ["booley", "flow", "custom", *tail])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr() == expected
