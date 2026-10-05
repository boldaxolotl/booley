"""Human vocabulary, checkout routing, and transport isolation contracts."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from booley.flows.builtin_cli import build_parser, parse_request
from booley.flows.cli_selection import (
    INVOCATION_ORIGIN_ENV,
    Duration,
    extract_project_tail,
    human_parser,
    invocation_context,
    normalize_endpoint_args,
    parse_duration,
    resolve_selection,
)
from booley.flows.lint.flow import LintFlow
from booley.harness import booley as cli
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.registry import discover_mcp_tools
from booley.specialists.reviewer import ReviewerSpecialist


def command(argv):
    parser = cli._build_parser()
    return cli._normalize_args(parser, parser.parse_args(argv))


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.delenv(INVOCATION_ORIGIN_ENV, raising=False)
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
        (root / ".booley_project").mkdir()
        (root / "rtl").mkdir()
        roots.append(root)
    monkeypatch.chdir(roots[0])
    return roots


@pytest.mark.parametrize(
    "value, milliseconds",
    [
        ("90", 90000),
        ("90s", 90000),
        ("30m", 1800000),
        ("2h", 7200000),
        ("1h30m", 5400000),
        ("1m30s", 90000),
        ("1m0s", 60000),
        ("999999999999999999999h", 999999999999999999999 * 3600000),
    ],
)
def test_duration_grammar(value, milliseconds):
    assert parse_duration(value) == Duration(milliseconds)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "0h0m0s",
        "-1",
        "+1",
        "1.5",
        "1ms",
        "NaN",
        "inf",
        " 1s",
        "1s ",
        "1h 2m",
        "1m1h",
        "1s1s",
        "1H",
        "s",
        "1h2",
        "1" * 4097,
    ],
)
def test_invalid_duration(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(value)


def walk(parser, route=()):
    yield route, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from walk(child, (*route, name))


def assert_controls(parser, *, timeout=False):
    controls = parser._option_string_actions
    assert not {"--directory", "--time-limit"} & controls.keys()
    assert {"-C", "--project"} <= controls.keys()
    for option in ("--project-root", "--work-dir", "--timeout-ms"):
        if option in controls:
            assert controls[option].help == argparse.SUPPRESS
        assert option not in parser.format_help()
    # A shared destination must not acquire another visible spelling.
    shared = {controls["--project"].dest, "timeout_ms", "dry_run", "verbose", "json"}
    approved = {
        "-C",
        "--project",
        "--project-root",
        "-p",
        "--work-dir",
        "--timeout",
        "--timeout-ms",
        "--dry-run",
        "-v",
        "--verbose",
        "--json",
    }
    for action in parser._actions:
        if action.dest in shared:
            assert set(action.option_strings) <= approved
    if timeout:
        assert controls["--timeout"].type is parse_duration


def test_recursive_command_inventory():
    routes = list(walk(cli._build_parser()))
    top = {route[0] for route, _ in routes if len(route) == 1}
    assert top == cli.COMMAND_PROJECT_BINDINGS.keys()
    for route, parser in routes:
        if route and (
            route[0] in {"run", "board"}
            or cli.COMMAND_PROJECT_BINDINGS[route[0]] is cli.ProjectBinding.INDEPENDENT
        ):
            assert "--project" not in parser._option_string_actions
        else:
            assert_controls(parser)
    # New nested routes require an explicit ownership/compatibility decision.
    nested = {
        "session": {"up", "enter", "down", "status", "validate", "prepare", "refresh"},
        "cleanup": {"prepare", "record", "preview", "apply"},
        "upgrade": {"status", "acknowledge"},
        "feedback": {
            "add",
            "friction",
            "say",
            "win",
            "triage",
            "filed",
            "list",
            "report",
            "export",
            "redact",
        },
    }
    actual = {route for route, _ in routes if len(route) > 1 and route[0] in nested}
    expected = {
        (owner, operation) for owner, operations in nested.items() for operation in operations
    }
    assert actual == expected


def test_detached_endpoint_inventory():
    for info in discover_mcp_tools():
        if info.kind not in {"flow", "specialist"}:
            continue
        endpoint = cli._load_mcp_tool_class(info)()
        if info.kind == "flow":
            parser = build_parser(endpoint, human=True)
        else:
            parser = human_parser(endpoint._parser, durations=True)
        assert_controls(parser, timeout=True)


@pytest.mark.parametrize(
    "argv",
    [
        ["-C", "../b", "targets"],
        ["targets", "--project", "../b"],
        ["session", "-C", "../b", "status"],
        ["session", "status", "--project=../b"],
        ["-C../b", "session", "status"],
        ["-C", "../b"],
        ["cleanup", "preview", "-C", "../b"],
        ["feedback", "list", "-C", "../b"],
        ["upgrade", "status", "-C", "../b"],
    ],
)
def test_project_positions(projects, argv):
    args = command(argv)
    assert cli._resolve_cli_selection(args) == projects[1]


@pytest.mark.parametrize(
    "argv",
    [
        ["-C", "a", "targets", "-C", "../b"],
        ["targets", "-C", "a", "-C", "../b"],
        ["session", "-C", "a", "down", "-p", "../b"],
        ["doctor", "--project-root", "a", "--project", "../b"],
        ["flow", "-C", "a", "lint", "-C", "../b"],
        ["flow", "lint", "--project=a", "--work-dir", "../b"],
        ["-C", "a", "bootstrap"],
        ["-C", "a", "run"],
        ["-C", "a", "board"],
    ],
)
def test_project_conflicts_and_exclusions(projects, argv):
    with pytest.raises(SystemExit) as error:
        command(argv)
    assert error.value.code == 2


def test_discovery_precedence_and_literal_aliases(projects, monkeypatch, capsys):
    a, b = projects
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(a))
    assert resolve_selection(b / "rtl") == b
    assert resolve_selection(b / "rtl", legacy=True) == b / "rtl"
    from booley.runtime.project_discovery import discover_project_root

    assert discover_project_root(b / "rtl") == a
    args = command(["doctor", "--project-r", str(b)])
    assert cli._resolve_cli_selection(args) == b
    assert "--project-root is deprecated" in capsys.readouterr().err


def test_cheat_bare_alias_and_selection(projects, capsys):
    args = command(["cheat", "--project", "--flows"])
    assert args.project and args.flows
    assert not hasattr(args, "_cli_selection")
    assert "use --project-files" in capsys.readouterr().err
    args = command(["cheat", "--project=../b", "--project-files"])
    assert args.project
    assert cli._resolve_cli_selection(args) == projects[1]


def test_session_prepare_legacy_selector_notice(projects, capsys):
    args = command(["session", "prepare", "--project-root", str(projects[1])])
    assert cli._resolve_cli_selection(args) == projects[1]
    assert capsys.readouterr().err.count("--project-root is deprecated") == 1
    with invocation_context(transport=True):
        args = command(["session", "prepare", "--project-root", str(projects[1])])
    assert cli._resolve_cli_selection(args) == projects[1]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "argv, field",
    [
        (["session", "enter", "--", "git", "-C", "x", "status"], "exec_cmd"),
        (["shell", "--", "tool", "--project", "x"], "shell_cmd"),
    ],
)
def test_payload_is_opaque(argv, field):
    args = command(argv)
    assert not hasattr(args, "_cli_selection")
    assert getattr(args, field) == argv[argv.index("--") :]


@pytest.mark.parametrize(
    "tail",
    [
        ["-C", "../b", "--target", "demo"],
        ["--target", "demo", "--project=../b"],
        ["--", "--target", "demo", "-C../b"],
        ["--timeout", "5s", "--target", "demo", "-C", "../b"],
    ],
)
def test_builtin_tail_selection_before_discovery(projects, tail):
    args = command(["flow", "lint", *tail])
    assert cli._resolve_cli_selection(args) == projects[1]
    assert not any(
        token.startswith("-C") or token.startswith("--project") for token in args.endpoint_args
    )


def test_extractor_preserves_values_and_boundaries():
    parser = human_parser(ReviewerSpecialist()._parser, durations=True)
    tails = [
        ["--instruction", "--project", "--scope", "rtl"],
        ["--instruction", "text --project b", "--", "--project", "../b"],
        ["--unknown", "--project", "../b"],
    ]
    for tail in tails:
        forwarded, selections = extract_project_tail(parser, tail)
        assert forwarded == tail
        assert selections == []


@pytest.mark.parametrize(
    "options, expected",
    [
        (["--timeout", "5s"], 5000),
        (["--timeout-ms", "5000"], 5000),
        (["--timeout-ms", "1"], 1),
        ([], None),
    ],
)
def test_flow_timeout_boundary(projects, options, expected, capsys):
    request = parse_request(LintFlow(), ["--target", "demo", *options])
    assert request.timeout_ms == expected
    assert not any(key.startswith("_cli_") for key in vars(request))
    assert bool(capsys.readouterr().err) == ("--timeout-ms" in options)


@pytest.mark.parametrize(
    "options",
    [
        ["--timeout", "5s", "--timeout-ms", "5000"],
        ["--timeout", "5s", "--timeout", "6s"],
        ["--timeout-ms", "1", "--timeout-ms", "2"],
    ],
)
def test_duplicate_timeout(options):
    with pytest.raises(SystemExit):
        parse_request(LintFlow(), ["--target", "demo", *options])


def test_transport_schema_and_cli_remain_unchanged(projects, monkeypatch, capsys):
    flow = LintFlow()
    before = flow_schema(flow)
    parse_request(flow, ["--project", "../b", "--target", "demo", "--timeout", "5s"])
    assert flow_schema(flow) == before
    monkeypatch.setenv(INVOCATION_ORIGIN_ENV, "transport")
    request = parse_request(
        flow, ["--work-dir", str(projects[1]), "--target", "demo", "--timeout-ms", "1"]
    )
    assert request.work_dir == projects[1]
    assert request.timeout_ms == 1
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit):
        parse_request(flow, ["--target", "demo", "--timeout", "5s"])


def test_scoped_selection_restores_and_has_no_synthetic_alias(projects, capsys):
    with invocation_context(projects[1]):
        request = parse_request(LintFlow(), ["--target", "demo"])
        assert request.work_dir == projects[1]
        with pytest.raises(SystemExit):
            parse_request(LintFlow(), ["--target", "demo", "-C", "a"])
    assert parse_request(LintFlow(), ["--target", "demo"]).work_dir == projects[0]
    assert "deprecated" not in capsys.readouterr().err


def test_plugin_owned_options_and_transport_are_preserved():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--work-dir", type=Path, default=Path.cwd())
    parser.add_argument("--project", choices=["short", "long"])
    parser.add_argument("--timeout", choices=["short", "long"])
    projection = human_parser(parser, durations=False)
    args = projection.parse_args(["--project", "short", "--timeout", "long"])
    assert args.project == "short" and args.timeout == "long"
    assert "-C" not in parser._option_string_actions
    normalize_endpoint_args(args)


def dispatch(argv, monkeypatch):
    monkeypatch.setattr(cli, "_parse_cli", lambda: command(argv))
    monkeypatch.setattr(cli, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)
    return cli.main()


@pytest.mark.parametrize(
    "route", ["targets", "chat", "doctor", "init", "session", "cleanup", "upgrade", "feedback"]
)
def test_handlers_receive_selected_checkout(projects, monkeypatch, route):
    captured = []
    monkeypatch.setitem(cli._EARLY_COMMANDS, route, lambda args, root: captured.append(root) or 0)
    nested = {"cleanup": ["preview"], "upgrade": ["status"]}.get(route, [])
    assert dispatch([route, *nested, "-C", "../b/rtl"], monkeypatch) == 0
    assert captured == [projects[1]]


@pytest.mark.parametrize("route, operation", [("auth", "--status"), ("cheat", "--list")])
def test_independent_operations_ignore_selection(projects, monkeypatch, route, operation):
    monkeypatch.setattr(cli, "_resolve_cli_selection", lambda _args: pytest.fail("discovery"))
    monkeypatch.setitem(cli._EARLY_COMMANDS, route, lambda _args, _root: 0)
    assert dispatch([route, "-C", "missing", operation], monkeypatch) == 0


@pytest.mark.parametrize("route", ["targets", "cheat", "init"])
def test_invalid_selection_fails_before_handler(projects, monkeypatch, route):
    monkeypatch.setitem(cli._EARLY_COMMANDS, route, lambda *_args: pytest.fail("handler called"))
    assert dispatch([route, "-C", "missing"], monkeypatch) == 2
    assert dispatch([route, "-C", ".git/HEAD"], monkeypatch) == 2


@pytest.mark.parametrize(
    "tail",
    [
        ["--project", "../b", "lint", "--target", "demo"],
        ["lint", "--target", "demo", "--project", "../b"],
    ],
)
def test_discovery_config_and_execution_use_same_checkout(projects, monkeypatch, tail):
    from booley.mcp.endpoint_config import get_endpoint_config

    (projects[0] / ".booley_project/booley.toml").write_text("[flows.lint]\ntimeout_ms = 1000\n")
    (projects[1] / ".booley_project/booley.toml").write_text("[flows.lint]\ntimeout_ms = 9000\n")
    discovered = []
    original = cli._discover_project_mcp_tools

    def discover(root):
        discovered.append(root)
        return original(root)

    executed = []

    def main(flow, argv):
        request = parse_request(flow, argv)
        _, config = get_endpoint_config(request.work_dir)
        executed.append((request.work_dir, config["lint"]["timeout_ms"]))
        return 0

    monkeypatch.setattr(cli, "_discover_project_mcp_tools", discover)
    monkeypatch.setattr(LintFlow, "main", main)
    assert dispatch(["flow", *tail], monkeypatch) == 0
    assert discovered == [projects[1]]
    assert executed == [(projects[1], 9000)]


def test_external_data_selection_and_cache_switch(projects, monkeypatch):
    from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir

    assert resolve_project_dir(start=projects[0]) == projects[0] / ".booley_project"
    selected = cli._resolve_cli_selection(command(["targets", "-C", "../b"]))
    assert resolve_project_dir(start=selected) == projects[1] / ".booley_project"
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(projects[0] / ".booley_project"))
    selected = cli._resolve_cli_selection(command(["targets", "-C", "../b"]))
    assert selected == projects[1]
    assert resolve_project_dir(start=selected) == projects[0] / ".booley_project"
    assert resolve_checkout_project_dir(selected) == projects[1] / ".booley_project"


def test_shared_plugin_outer_selection_and_override_refusal(projects, capsys):
    from booley.flows.endpoint_context import EndpointContext
    from booley.mcp.base import McpTool

    class Common(McpTool):
        name = "example"
        description = "Example"

        def _add_args(self, parser):
            parser.add_argument("--project", choices=["short", "long"])

        def _run(self):
            raise AssertionError("not executing")

    endpoint = Common()
    with invocation_context(projects[1]):
        endpoint.parse_args(["--project", "short"])
    assert endpoint.args.work_dir == projects[1]
    assert endpoint.args.project == "short"

    class Override(Common):
        def main(self, argv=None):
            pytest.fail("override executed")

    args = argparse.Namespace(_cli_endpoint_project=projects[1])
    assert cli._invoke_endpoint(Override(), [], args) == 2
    assert "overrides the shared CLI" in capsys.readouterr().err
    assert Common.main is EndpointContext.main


def test_complete_specialist_schemas_are_compatible():
    import json

    expected = json.loads(
        (Path(__file__).parents[1] / "mcp_tools/fixtures/specialist_schemas.json").read_text()
    )
    for info in discover_mcp_tools():
        if info.kind != "specialist":
            continue
        endpoint = cli._load_mcp_tool_class(info)()
        assert flow_schema(endpoint) == expected[info.name]
        human_parser(endpoint._parser, durations=True)
        assert flow_schema(endpoint) == expected[info.name]


def test_cheat_help_advertises_path_and_new_section_only():
    cheat = dict(walk(cli._build_parser()))[("cheat",)]
    help_text = cheat.format_help()
    assert "--project PATH" in help_text
    assert "--project [PATH]" not in help_text
    assert "--project-files" in help_text


def test_colliding_plugin_cannot_override_outer_checkout(projects):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--work-dir", type=Path, default=projects[0])
    parser.add_argument("--project", choices=["short", "long"])
    with invocation_context(projects[1]):
        projected = human_parser(parser, durations=False)
        with pytest.raises(SystemExit):
            projected.parse_args(["--work-dir", str(projects[0])])
        assert projected.parse_args(["--project", "short"]).work_dir == projects[1]


def test_bare_cheat_alias_warns_once_per_invocation(capsys):
    args = command(["cheat", "--project", "--project"])
    assert args.project
    assert capsys.readouterr().err.count("is deprecated") == 1
    assert not any(key.startswith("_cli_") for key in vars(args))


def test_human_dispatch_context_overrides_inherited_transport_marker(
    projects, monkeypatch, capsys
):
    monkeypatch.setenv(INVOCATION_ORIGIN_ENV, "transport")
    executed = []

    def main(flow, argv):
        request = parse_request(flow, argv)
        executed.append((request.work_dir, request.timeout_ms))
        return 0

    monkeypatch.setattr(LintFlow, "main", main)
    assert (
        dispatch(
            ["flow", "-C", "../b", "lint", "--target", "demo", "--timeout", "5s"], monkeypatch
        )
        == 0
    )
    assert executed == [(projects[1], 5000)]
    # Context exit restores the subprocess marker's transport interpretation.
    request = parse_request(LintFlow(), ["--target", "demo", "--timeout-ms", "1"])
    assert request.timeout_ms == 1


def test_human_main_alias_notice_overrides_inherited_transport_marker(
    projects, monkeypatch, capsys
):
    monkeypatch.setenv(INVOCATION_ORIGIN_ENV, "transport")
    monkeypatch.setitem(cli._EARLY_COMMANDS, "doctor", lambda _args, _root: 0)
    assert dispatch(["doctor", "--project-root", str(projects[1])], monkeypatch) == 0
    assert "--project-root is deprecated" in capsys.readouterr().err


@pytest.mark.parametrize("quiet", ["-q", "--quiet"])
def test_human_flow_progress_flags_preserve_project_and_duration_vocabulary(
    projects, quiet, capsys
):
    flow = LintFlow()
    before = flow_schema(flow)
    request = parse_request(
        flow, ["--target", "demo", "--project", "../b", "--timeout", "5s", quiet]
    )
    assert request.work_dir == projects[1]
    assert request.timeout_ms == 5000
    assert not any(key.startswith(("_cli_", "_console")) for key in vars(request))
    assert flow_schema(flow) == before
    assert capsys.readouterr().err == ""
