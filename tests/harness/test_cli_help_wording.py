"""Every ``booley … --help`` reads for a design engineer, not for an agent."""

from __future__ import annotations

import argparse
import copy
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_cli_parser
from booley.flows.cli_help import (
    EXAMPLE_HEADING,
    CommandHelpFormatter,
    SurfaceHelp,
    present_command,
)
from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV, CliInvocation
from booley.flows.endpoint_cli import human_endpoint_parser
from booley.harness import booley as cli
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.registry import discover_mcp_tools

# Internal vocabulary that must not leak into human help.
BANNED = re.compile(r"\bADR \d+|\bMCP\b|\bendpoint|mode=elab_only", re.IGNORECASE)
# (command, exact phrase) -> why this one use is intended. Only the phrase is
# exempt, so another banned word on the same page still fails.
ALLOWED = {
    ("booley flow synth", "violating-endpoint repair percentage"): (
        "--repair-tns-percent names timing-path endpoints, the static-timing term "
        "OpenROAD itself uses"
    ),
}
EXPECTED_ENDPOINTS = {
    ("flow", "sim"),
    ("flow", "lint"),
    ("flow", "synth"),
    ("flow", "fpga"),
    ("specialist", "coverage_analyst"),
    ("specialist", "mutation_tester"),
    ("specialist", "reviewer"),
}


# These exact recovery contracts intentionally name the protocol required by #1549.
ALLOWED.update(
    {
        (
            f"booley {kind} {name}",
            "CLI runs never record Goal evidence; use Goal-bound MCP calls with work_dir.",
        ): "Goal recording recovery must name the supported MCP call and work_dir"
        for kind, name in EXPECTED_ENDPOINTS
    }
)
ALLOWED[
    (
        "booley specialist coverage_analyst",
        "Only Goal-bound MCP calls record proposed coverage waivers for your review.",
    )
] = "CLI coverage advice must not promise recording Goal waiver candidates"


def _endpoints() -> dict[str, object]:
    """Instantiate every built-in Flow and Specialist reachable from the CLI."""
    found = {}
    for info in discover_mcp_tools():
        if info.kind not in {"flow", "specialist"}:
            continue
        endpoint_type = cli._load_mcp_tool_class(info)
        assert endpoint_type is not None, info.name
        found[f"booley {info.kind} {info.name}"] = endpoint_type()
    assert {tuple(command.split()[1:]) for command in found} == EXPECTED_ENDPOINTS
    return found


ENDPOINTS = _endpoints()


@pytest.mark.parametrize("inside", [False, True])
@pytest.mark.parametrize("prefix", [[], ["-C", "missing"], ["--project", "help"]])
def test_help_command_matches_help_flag_without_project(
    inside: bool,
    prefix: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)

    def unexpected(*_args, **_kwargs):
        pytest.fail("help touched Project/runtime state")

    monkeypatch.setattr(cli, "_selected_project_root", unexpected)
    monkeypatch.setattr(cli, "_enforce_runtime_location", unexpected)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", unexpected)
    outputs = []
    for arguments in (["--help"], ["-h"], ["help"], ["help", "--help"], ["help", "-h"]):
        monkeypatch.setattr("sys.argv", ["booley", *prefix, *arguments])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        outputs.append(captured.out)

    assert outputs[0].startswith("usage: booley ")
    assert all(output == outputs[0] for output in outputs)
    assert list(tmp_path.iterdir()) == []


def _human_parser(endpoint: object) -> argparse.ArgumentParser:
    if isinstance(endpoint, BuiltinFlow):
        return build_cli_parser(endpoint, CliInvocation())
    return human_endpoint_parser(endpoint)


def _walk(parser: argparse.ArgumentParser, route: tuple[str, ...]):
    yield " ".join(route), parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _walk(child, (*route, name))


def _all_help() -> dict[str, str]:
    """Render the top level, every subcommand, and every Flow and Specialist."""
    pages = {
        command: parser.format_help()
        for command, parser in _walk(cli._build_parser(), ("booley",))
    }
    assert {"booley", "booley flow", "booley specialist", "booley doctor"} <= pages.keys()
    for command, endpoint in ENDPOINTS.items():
        assert command not in pages
        pages[command] = _human_parser(endpoint).format_help()
    return pages


def test_help_has_no_internal_vocabulary() -> None:
    pages = _all_help()
    assert len(pages) > len(ENDPOINTS) + 20  # the walk really descended the command tree
    flat = {command: " ".join(text.split()) for command, text in pages.items()}
    stale = sorted(key for key in ALLOWED if key[1] not in flat[key[0]])
    assert not stale, f"allowlist entries no longer needed: {stale}"
    for command, phrase in ALLOWED:
        assert BANNED.search(phrase), f"allowlisted phrase has no banned token: {phrase}"
        flat[command] = flat[command].replace(phrase, "")
    leaked = sorted(
        (command, match.group(0))
        for command, text in flat.items()
        for match in BANNED.finditer(text)
    )
    assert not leaked, (
        f"internal vocabulary in --help (reword it or allowlist with a reason): {leaked}"
    )
    assert all(reason.strip() for reason in ALLOWED.values())


@pytest.mark.parametrize("command", sorted(ENDPOINTS))
def test_usage_shows_full_command(command: str) -> None:
    help_text = _human_parser(ENDPOINTS[command]).format_help()
    assert help_text.startswith(f"usage: {command} ")


@pytest.mark.parametrize("command", sorted(ENDPOINTS))
def test_description_and_example_are_written_for_the_command_line(command: str) -> None:
    endpoint = ENDPOINTS[command]
    declared = endpoint.cli_help
    assert declared is not None
    assert declared.summary != endpoint.description
    assert declared.example.startswith(command + " --")

    parser = _human_parser(endpoint)
    help_text = parser.format_help()
    assert " ".join(declared.summary.split()) in " ".join(help_text.split())
    # The example stays one copyable line even when the terminal is narrow.
    parser.formatter_class = lambda prog: CommandHelpFormatter(prog, width=40)
    assert f"{EXAMPLE_HEADING}\n  {declared.example}\n" in parser.format_help()
    # The example is a command this parser accepts.
    arguments = shlex.split(declared.example)[len(command.split()) :]
    parser.parse_args(arguments)


@pytest.mark.parametrize("command", ["flow", "specialist"])
def test_dispatch_positionals_use_user_words(command: str) -> None:
    parser = dict(_walk(cli._build_parser(), ("booley",)))[f"booley {command}"]
    help_text = parser.format_help()
    assert help_text.startswith(f"usage: booley {command} ")
    assert f"[{command}] ..." in help_text.splitlines()[0]
    assert re.search(rf"^  {command}\s", help_text, re.MULTILINE)
    assert re.search(r"^  args\s", help_text, re.MULTILINE)
    assert "endpoint_name" not in help_text
    assert "endpoint_args" not in help_text
    args = parser.parse_args(["name", "--flag"])
    assert (args.endpoint_name, args.endpoint_args) == ("name", ["--flag"])


def _schema_text(command: str, parameter: str) -> str:
    return flow_schema(ENDPOINTS[command])["properties"][parameter]["description"]


@pytest.mark.parametrize(
    "command, parameter, agent_note, human_note",
    [
        ("booley flow sim", "target", "MCP accepts one comma-separated string", "Repeat the flag"),
        ("booley flow sim", "test", "MCP: array", "repeat the flag for more"),
        ("booley specialist reviewer", "steer", "over MCP pass an array", "Repeat the flag"),
        (
            "booley specialist mutation_tester",
            "target",
            "MCP accepts one Target selector string",
            "whose tests judge each mutation",
        ),
    ],
)
def test_agent_schema_keeps_notes_removed_from_help(
    command: str, parameter: str, agent_note: str, human_note: str
) -> None:
    schema_text = _schema_text(command, parameter)
    assert type(schema_text) is str
    assert agent_note in schema_text
    assert human_note not in schema_text
    help_text = " ".join(_human_parser(ENDPOINTS[command]).format_help().split())
    assert human_note in help_text
    assert agent_note not in help_text


def test_agent_tool_description_keeps_parameter_spelling() -> None:
    sim = next(info for info in discover_mcp_tools() if info.name == "sim")
    assert "mode=elab_only" in sim.description
    assert "MCP tool" in sim.description
    assert sim.description == ENDPOINTS["booley flow sim"].description


def test_surface_help_survives_parser_copy() -> None:
    text = SurfaceHelp("for agents", cli="for people")
    copied = copy.deepcopy(text)
    assert (copied, copied.cli, type(copied)) == ("for agents", "for people", SurfaceHelp)


def test_extension_without_cli_wording_keeps_its_description() -> None:
    class Extension:
        endpoint_kind = "flow"
        name = "custom"

    parser = argparse.ArgumentParser(prog="custom", description="Custom Flow text")
    parser.add_argument("--plain", help="Plain help")
    presented = present_command(parser, Extension())
    help_text = presented.format_help()
    assert help_text.startswith("usage: booley flow custom ")
    assert "Custom Flow text" in help_text
    assert "Plain help" in help_text
    assert EXAMPLE_HEADING not in help_text


@pytest.mark.parametrize("command", sorted(ENDPOINTS))
@pytest.mark.parametrize("inside", [False, True])
@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("prefix", [[], ["--diagnostic"], ["--"]])
def test_help_from_the_booley_entry_point(
    command: str,
    inside: bool,
    flag: str,
    prefix: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wording reaches a person who types the command inside a Project."""
    for name in ("RTL_PROJECT_ROOT", "BOOLEY_DIR", INVOCATION_ORIGIN_ENV):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".booley").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    monkeypatch.setattr("sys.argv", [*command.split(), *prefix, flag])
    monkeypatch.setattr(
        cli, "_invoke_endpoint", lambda *_args, **_kwargs: pytest.fail("help executed")
    )
    monkeypatch.setattr(
        cli.runtime_context, "ensure_proxy_env", lambda: pytest.fail("help restored proxy")
    )

    with pytest.raises(SystemExit) as baseline_exit:
        ENDPOINTS[command].main([flag])
    assert baseline_exit.value.code == 0
    baseline = capsys.readouterr()

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert captured == baseline
    help_text = captured.out
    assert help_text.startswith(f"usage: {command} ")
    assert f"{EXAMPLE_HEADING}\n  {ENDPOINTS[command].cli_help.example}\n" in help_text
    checked = " ".join(help_text.split())
    for route, phrase in ALLOWED:
        if route == command:
            checked = checked.replace(phrase, "")
    assert not BANNED.search(checked)
    if command.startswith("booley flow "):
        assert "--quiet" in help_text
