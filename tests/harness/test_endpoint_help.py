"""Help flags cannot admit Flow or Specialist execution from the host."""

from __future__ import annotations

import argparse
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from booley.harness import booley as cli
from booley.harness.endpoint_help import _parse_help, _parser_copy, _ParserSyntaxError
from booley.mcp.base import McpTool
from booley.mcp.registry import McpToolInfo
from booley.runtime.booley_dir import reset_cache


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
    cli_exit,
) -> None:
    import booley.runtime.layout_consumers as consumers

    monkeypatch.setattr(
        consumers, "directory_consumer", lambda *_a: pytest.fail("execution admission")
    )
    route = [command] if not tail else [command, name, *tail]
    captured = cli_exit(["booley", *route], code=2)
    assert "runs inside the Booley Sandbox" in captured.err


@pytest.fixture
def cli_exit(monkeypatch, capsys):
    """Invoke the CLI and check its exit before returning captured streams."""

    def run(argv, *, code):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == code
        return capsys.readouterr()

    return run


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
    cli_exit,
) -> None:
    register_custom(monkeypatch, Custom)
    # Argparse and the dispatcher each consume one leading separator.
    # The final separator belongs to the custom parser's literal payload.
    captured = cli_exit(["booley", "flow", "custom", "--", "--", "--", "--help"], code=2)
    assert captured.out == ""
    assert "runs inside the Booley Sandbox" in captured.err


def assert_number_error(captured):
    assert captured.out == ""
    assert captured.err.count("usage:") == 1
    assert "invalid int value" in captured.err
    assert "runs inside" not in captured.err


def test_parser_error_is_reported_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
) -> None:
    register_custom(monkeypatch, Custom)
    captured = cli_exit(["booley", "flow", "custom", "--number", "bad", "--help"], code=2)
    assert_number_error(captured)


@pytest.mark.parametrize("exit_code", [0, 2, 7])
def test_custom_exit_is_not_help(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
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
    captured = cli_exit(["booley", "flow", "custom", "--version", "--help"], code=2)
    assert captured.out == ""
    assert "runs inside the Booley Sandbox" in captured.err
    assert calls == ["version"]


def test_named_help_retains_metadata_admission(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
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
    captured = cli_exit(["booley", "flow", "lint", "--target", "demo", "--help"], code=0)
    assert "usage: booley flow lint" in captured.out
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
    cli_exit,
) -> None:
    captured = cli_exit(["booley", "flow", "lint", "-qh"], code=0)
    assert "--quiet" in captured.out


def test_help_named_option_is_not_a_help_action(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
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
    captured = cli_exit(["booley", "flow", "custom", "--help"], code=2)
    assert "runs inside the Booley Sandbox" in captured.err


def hinted_endpoint(endpoint_type, calls, *, nested):
    class Hint(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Any,
            option_string: str | None = None,
        ) -> None:
            calls.append("hint")

    class Hinted(endpoint_type):
        def _add_args(self, parser):
            super()._add_args(parser)
            if nested:
                commands = next(
                    action
                    for action in parser._actions
                    if isinstance(action, argparse._SubParsersAction)
                )
                parser = commands.choices["nested"]
            parser.add_argument("--hint", action=Hint, nargs=0)

    return Hinted


def test_help_looking_data_is_parsed_once_in_sandbox(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    Hinted = hinted_endpoint(Custom, calls, nested=False)

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


def test_inert_probe_skips_conversion_of_unseen_string_defaults() -> None:
    def reject(value: str) -> str:
        raise argparse.ArgumentTypeError(value)

    parser = argparse.ArgumentParser()
    parser.add_argument("--value", type=reject, default="sandbox-only")
    assert _parse_help(_parser_copy(parser, inert=True), [])[0] is None
    assert isinstance(_parse_help(_parser_copy(parser, inert=False), [])[0], _ParserSyntaxError)


def test_missing_project_error_precedes_host_venue_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reset_cache()
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
    cli_exit,
    name: str,
) -> None:
    register_custom(monkeypatch, Nested)
    captured = cli_exit(["booley", "flow", "custom", name, "--help"], code=0)
    assert "nested" in captured.out
    assert "--number" in captured.out
    assert captured.err == ""


def test_nested_syntax_error_is_reported_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
) -> None:
    register_custom(monkeypatch, Nested)
    captured = cli_exit(
        ["booley", "flow", "custom", "nested", "--number", "bad", "--help"], code=2
    )
    assert_number_error(captured)


def test_nested_custom_action_runs_once_before_help(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
) -> None:
    calls = []

    Hinted = hinted_endpoint(Nested, calls, nested=True)

    register_custom(monkeypatch, Hinted)
    captured = cli_exit(["booley", "flow", "custom", "nested", "--hint", "--help"], code=0)
    assert captured.err == ""
    assert calls == ["hint"]


class Expand(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        parser.add_argument("--late-option", help="Added by the argument action")
        parser.add_argument("late_payload", nargs="?", help="Added positional")


class ReplaceHelp(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        parser.add_argument("-h", "--help", action="store_true", help="Plugin option")


def feature_endpoint(feature, nested):
    class Featured(Custom):
        def _add_args(self, parser):
            if nested:
                parser = parser.add_subparsers().add_parser("nested")
            if feature == "registered":
                parser.register("type", "hexadecimal", lambda value: int(value, 16))
                parser.add_argument("--number", type="hexadecimal")
            elif feature == "grouped":
                group = parser.add_argument_group("Custom group")
                exclusive = group.add_mutually_exclusive_group()
                exclusive.add_argument("--one", action="store_true")
                exclusive.add_argument("--two", action="store_true")
            elif feature == "expanding":
                parser.add_argument("--expand", action=Expand, nargs=0)
            else:
                parser.conflict_handler = "resolve"
                parser._optionals.conflict_handler = "resolve"
                parser.add_argument("--replace-help", action=ReplaceHelp, nargs=0)

    return Featured


@pytest.mark.parametrize(
    "feature,nested,inside",
    [("registered", nested, inside) for nested in (False, True) for inside in (False, True)]
    + [
        (feature, nested, False)
        for feature in ("grouped", "expanding")
        for nested in (False, True)
    ]
    + [("replacing", False, False)],
)
def test_extension_help_matches_direct_parser(
    project, monkeypatch, capsys, cli_exit, feature, nested, inside
):
    from booley.flows.endpoint_cli import human_endpoint_parser

    endpoint_type = feature_endpoint(feature, nested)
    register_custom(monkeypatch, endpoint_type)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    options, descriptions = {
        "registered": (["--number", "ff"], []),
        "grouped": ([], ["Custom group:"]),
        "expanding": (["--expand"], ["Added by the argument action", "Added positional"]),
        "replacing": (["--replace-help"], ["Plugin option"]),
    }[feature]
    tail = (["nested"] if nested else []) + options + ["--help"]
    with pytest.raises(SystemExit) as direct:
        human_endpoint_parser(endpoint_type()).parse_args(tail)
    assert direct.value.code == 0
    expected = capsys.readouterr()
    for description in descriptions:
        assert description in expected.out
    captured = cli_exit(["booley", "flow", "custom", *tail], code=0)
    assert captured == expected


@pytest.mark.parametrize("command", ["flow", "specialist"])
def test_unknown_endpoint_help_cannot_list_from_host(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_exit,
    command: str,
) -> None:
    captured = cli_exit(["booley", command, "unknown-endpoint", "--help"], code=2)
    assert captured.out == ""
    assert "Available" not in captured.err
    assert "runs inside the Booley Sandbox" in captured.err


def test_diagnostic_help_is_truthful_only_on_human_surface():
    from booley.flows.builtin_cli import build_parser
    from booley.flows.lint.flow import LintFlow

    human = " ".join(build_parser(LintFlow(), human=True).format_help().split())
    transport = build_parser(LintFlow()).format_help()
    assert "CLI runs never record Goal evidence" in human
    assert "Run without satisfying Goals" in transport
    assert "CLI runs never" not in transport
