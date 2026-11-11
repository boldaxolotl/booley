"""Human help remains complete without changing transport or parsing contracts."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_cli_parser, build_parser
from booley.flows.cli_help import FlowHelpFormatter, shared_help
from booley.flows.cli_selection import CliInvocation, normalize_endpoint_args
from booley.flows.endpoint_cli import normalize_target_arg
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.registry import discover_mcp_tools
from tests.subprocess_budget import run_bounded_child


def builtin_types() -> list[type[BuiltinFlow]]:
    discovered = [info for info in discover_mcp_tools() if info.kind == "flow"]
    assert discovered
    result = []
    for info in discovered:
        module = importlib.import_module(
            "booley." + info.path.removesuffix(".py").replace("/", ".")
        )
        matches = [
            cls
            for cls in vars(module).values()
            if isinstance(cls, type)
            and issubclass(cls, BuiltinFlow)
            and cls is not BuiltinFlow
            and cls.name == info.name
        ]
        assert len(matches) == 1
        result.extend(matches)
    return result


FLOWS = builtin_types()
PAIRS = (
    "flatten",
    "generic-abc-before-mapping",
    "repair-setup",
    "repair-hold",
    "gate-cloning",
)
# These are the entire human compatibility exception, not a blanket SUPPRESS exemption.
HIDDEN = {"--work-dir", "--timeout-ms", "--elab-only", "--build-only", "--standalone"}


@pytest.mark.parametrize("flow_type", FLOWS)
@pytest.mark.parametrize("cli", [False, True])
def test_complete_grouped_help(flow_type: type[BuiltinFlow], cli: bool) -> None:
    flow = flow_type()
    parser = build_cli_parser(flow, CliInvocation()) if cli else build_parser(flow, human=True)
    groups = (
        shared_help(flow.name, target_required=flow.target_required)
        + flow.argument_adapter.help_groups
    )
    metadata = {option.dest for group in groups for option in group.options}
    assert metadata == {a.dest for a in parser._actions} - {"help", "_console_quiet"}
    named = [
        g for g in parser._action_groups if g.title not in {"positional arguments", "options"}
    ]
    assert [g.title for g in named[:2]] == ["Common", "Output"]
    body = parser.format_help().split("options:\n", 1)[1]
    positions = [body.index(group.title + ":") for group in named]
    assert positions == sorted(positions)
    for action in parser._actions:
        owners = [g for g in parser._action_groups if action in g._group_actions]
        assert len(owners) == 1
        if action.dest == "help":
            assert owners[0].title == "options"
            continue
        assert owners[0] in named
        if action.help == argparse.SUPPRESS:
            assert set(action.option_strings) <= HIDDEN
            assert all(option not in body for option in action.option_strings)
        else:
            assert action.help and any(
                word in action.help for word in ("omitted", "Default:", "Required:")
            )
            assert action.option_strings[0] in body
    assert {"--help-all", "--help-expert", "--cache", "--waivers", "--kill"}.isdisjoint(
        parser._option_string_actions
    )
    if flow.name == "synth":
        assert [g.title for g in named[-2:]] == ["Expert: ABC", "Expert: OpenROAD"]
        assert all("Defaults are tuned" in g.description for g in named[-2:])


@pytest.mark.parametrize("width", [46, 100])
def test_pair_rendering(width: int) -> None:
    flow = next(cls() for cls in FLOWS if cls.name == "synth")
    parser = build_parser(flow, human=True)
    pairs = tuple((f"--{name}", f"--no-{name}") for name in PAIRS)
    parser.formatter_class = lambda prog: FlowHelpFormatter(prog, pairs=pairs, width=width)
    body = parser.format_help().split("options:\n", 1)[1]
    for positive, negative in pairs:
        assert body.count(f"{positive}, {negative}") == 1
        assert not re.search(rf"^  {re.escape(negative)}(?:\s|$)", body, re.MULTILINE)
        actions = parser._option_string_actions
        assert actions[positive].help == actions[negative].help
        normalized = " ".join(body.split())
        assert normalized.count(" ".join(actions[positive].help.split())) == 1


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("name", PAIRS)
@pytest.mark.parametrize(
    "suffix,expected",
    [([], None), ([True], True), ([False], False), ([True, False], False), ([False, True], True)],
)
def test_pair_parsing_and_typed_normalization(
    human: bool, name: str, suffix: list[bool], expected: bool | None
) -> None:
    flow = next(cls() for cls in FLOWS if cls.name == "synth")
    parser = build_parser(flow, human=human)
    flags = [f"--{'' if value else 'no-'}{name}" for value in suffix]
    args = parser.parse_args(["--target", "synth_example", *flags])
    assert getattr(args, name.replace("-", "_")) is expected
    normalize_endpoint_args(args)
    normalize_target_arg(args)
    flow.argument_adapter.normalize(args, parser)
    request = flow.request_type(**vars(args))
    assert getattr(request, name.replace("-", "_")) is expected


@pytest.mark.parametrize("flow_type", FLOWS)
@pytest.mark.parametrize("human_first", [False, True])
def test_projection_isolation(flow_type: type[BuiltinFlow], human_first: bool) -> None:
    flow = flow_type()
    expected = json.loads((Path(__file__).parent / "fixtures/builtin_schemas.json").read_text())[
        flow.name
    ]
    parsers = []
    for _ in range(2):
        for human in (human_first, not human_first):
            parser = build_parser(flow, human=human)
            parser.format_help()
            parsers.append(parser)
            assert flow_schema(flow) == expected
    assert parsers[0].format_help() == parsers[2].format_help()
    assert parsers[1].format_help() == parsers[3].format_help()
    transport = build_cli_parser(flow, CliInvocation(transport=True))
    assert "--work-dir" in transport._option_string_actions
    assert "--project" not in transport._option_string_actions
    assert transport.parse_args(["--target", "example", "-q"])._console_quiet


def test_resume_and_required_target_guidance() -> None:
    for cls in FLOWS:
        parser = build_parser(cls(), human=True)
        actions = parser._option_string_actions
        if cls.name == "sim":
            assert "manifest owns" in actions["--target"].help
            for flag in ("--coverage", "--trace", "--no-waivers"):
                assert "resume restores the manifest" in actions[flag].help
                assert "rejects this flag" in actions[flag].help
            assert "omission is required" in actions["--mode"].help
        else:
            assert "exactly one eligible Target" in actions["--target"].help
            assert "zero or multiple" in actions["--target"].help
            assert not actions["--target"].required
            parser.parse_args([])


TARGET_OMISSION = "When omitted, exactly one eligible Target is selected and announced"
DEFAULT_MARKERS = ("When omitted", "Default:", "(default ")


def test_target_help_states_each_rule_once() -> None:
    """Every Built-in Flow's human `--target` help says the omission rule once."""
    for cls in FLOWS:
        flow = cls()
        parsers = (build_parser(flow, human=True), build_cli_parser(flow, CliInvocation()))
        for parser in parsers:
            help_text = parser._option_string_actions["--target"].help
            assert help_text.count(TARGET_OMISSION) == 1, (flow.name, help_text)
            if flow.name == "sim":
                assert help_text.count("manifest owns") == 1, help_text
                assert help_text.count("conflicts with --resume-from") == 1, help_text
        # Check the rendered text too; whitespace-normalize since argparse wraps lines.
        rendered = " ".join(parsers[0].format_help().split())
        assert rendered.count(TARGET_OMISSION) == 1, flow.name


def test_human_help_states_each_default_once() -> None:
    """Each human option's omission/default text appears once, not in its description too."""
    for cls in FLOWS:
        flow = cls()
        parser = build_parser(flow, human=True)
        groups = (
            shared_help(flow.name, target_required=flow.target_required)
            + flow.argument_adapter.help_groups
        )
        for group in groups:
            for option in group.options:
                # Several actions can share a dest (hidden alias + visible flag): check the visible ones.
                for action in parser._actions:
                    if action.dest != option.dest or action.help == argparse.SUPPRESS:
                        continue
                    where = (flow.name, option.dest)
                    assert action.help.count(option.omission) == 1, (where, action.help)
                    description = action.help.removesuffix(option.omission)
                    for marker in DEFAULT_MARKERS:
                        assert marker not in description, (where, marker, action.help)


def test_module_help_outside_project(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BOOLEY_", "RTL_", "_BOOLEY_"))
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = run_bounded_child(
        [sys.executable, "-m", "booley.flows.synth", "--help"],
        cwd=tmp_path,
        env=env,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Common:" in result.stdout and "Expert: OpenROAD:" in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", PAIRS[1:])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("explicit", [None, False, True])
def test_expert_pairs_defer_and_override_configuration(
    name: str, configured: bool, explicit: bool | None
) -> None:
    from booley.flows.synth.mode import SynthMode
    from booley.flows.synth.ppa_config import append_ppa_args
    from booley.flows.synth.request import SynthRequest

    dest = name.replace("-", "_")
    section = (
        "advanced_settings_yosys"
        if name == "generic-abc-before-mapping"
        else "advanced_settings_openroad"
    )
    cmd = []
    append_ppa_args(
        cmd,
        {section: {dest: configured}},
        SynthRequest(target="synth_example", **{dest: explicit}),
        synth_mode=SynthMode.PHYSICAL,
    )
    flags = [flag for flag in cmd if flag in {f"--{name}", f"--no-{name}"}]
    expected = configured if explicit is None else explicit
    assert flags[-1] == f"--{'' if expected else 'no-'}{name}"
