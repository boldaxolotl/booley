"""Paired ``--x``/``--no-x`` boolean options over MCP (issue #2803).

The MCP schema must describe the positive option with no default, and
``false`` must translate to the negative flag so it can override a Target
default instead of being dropped.
"""

from __future__ import annotations

import argparse
import collections
from pathlib import Path
from typing import Any

import pytest

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.synth.recipe import synthesis_recipe_args
from booley.mcp import catalog, server
from booley.mcp.call_context import resolve_call_context
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.registry import discover_mcp_tools

SYNTH_PAIR_DESTS = {
    "flatten",
    "generic_abc_before_mapping",
    "repair_setup",
    "repair_hold",
    "gate_cloning",
}
FLATTEN_HELP = "Flatten the hierarchy before tech-mapping (overrides the selected Target's flow_options.flatten)."


def _option_pairs(parser: argparse.ArgumentParser) -> dict[str, tuple[Any, Any]]:
    """Find same-dest ``store_true``/``store_false`` option pairs, independently of the code."""
    pairs: dict[str, tuple[Any, Any]] = {}
    for positive in parser._actions:
        if not (isinstance(positive, argparse._StoreTrueAction) and positive.option_strings):
            continue
        negatives = [
            action
            for action in parser._actions
            if isinstance(action, argparse._StoreFalseAction)
            and action.option_strings
            and action.dest == positive.dest
        ]
        if negatives:
            pairs[positive.dest] = (positive, negatives[0])
    return pairs


def test_synth_flatten_schema_describes_flattening_without_default() -> None:
    """AC1: ``flatten`` reads as flattening and carries no default."""
    prop = flow_schema(AsicSynthesizeFlow())["properties"]["flatten"]

    assert prop == {"type": "boolean", "description": FLATTEN_HELP}


@pytest.mark.parametrize("target_flatten", [True, False])
@pytest.mark.parametrize("mcp_value", [True, False, None], ids=["true", "false", "omitted"])
def test_mcp_flatten_reaches_synth_recipe(mcp_value: bool | None, target_flatten: bool) -> None:
    """AC2: false disables, true enables, omitted follows the Target."""
    definition, error = catalog._import_and_extract("flow", import_prefix="booley.flows.synth")
    assert error is None and definition is not None
    arguments: dict[str, Any] = {"target": "t"}
    if mcp_value is not None:
        arguments["flatten"] = mcp_value

    command = server._endpoint_command(
        "synth", arguments, definition, collections.defaultdict(int), resolve_call_context({})
    )
    # Argv follows the Flow runner's endpoint name, or the module under ``-m``.
    module_index = next(
        index
        for index, token in enumerate(command)
        if token in {"synth", definition["module_path"]}
    )
    args = build_parser(AsicSynthesizeFlow()).parse_args(command[module_index + 1 :])
    recipe = synthesis_recipe_args(
        {"synth_mode": "logical", "flatten": target_flatten}, args, target="t"
    )[0]

    expected = target_flatten if mcp_value is None else mcp_value
    assert recipe == ("--flatten" if expected else "--no-flatten")


def test_every_paired_option_on_every_mcp_tool() -> None:
    """AC3: each paired option has one described, default-free, two-way property."""
    seen: dict[str, set[str]] = {}
    for info in discover_mcp_tools():
        stem = Path(info.path).stem
        prefix = "booley." + Path(info.path).parent.as_posix().replace("/", ".")
        module, error = catalog._import_module_safe(f"{prefix}.{stem}")
        assert error is None, error
        found = catalog._find_mcp_tool_class_in_module(module, stem)
        if found is None:
            continue
        _cls, instance, schema = found
        parser = build_parser(instance) if isinstance(instance, BuiltinFlow) else instance._parser
        definition, error = catalog._import_and_extract(stem, import_prefix=prefix)
        assert error is None and definition is not None
        flags = definition["negative_flags"]
        for dest, (positive, negative) in _option_pairs(parser).items():
            if dest not in schema["properties"]:
                continue  # Filtered out of the public surface.
            seen.setdefault(info.name, set()).add(dest)
            prop = schema["properties"][dest]
            assert prop["type"] == "boolean", (info.name, dest)
            assert "default" not in prop, (info.name, dest)
            assert prop["description"], (info.name, dest)
            assert prop["description"] == positive.help, (info.name, dest)
            assert prop["description"] != negative.help, (info.name, dest)
            assert flags[dest] == negative.option_strings[0], (info.name, dest)
            assert server._params_to_argv({dest: True}, flags) == [positive.option_strings[0]]
            assert server._params_to_argv({dest: False}, flags) == [negative.option_strings[0]]
            assert server._params_to_argv({}, flags) == []

    assert seen.get("synth") == SYNTH_PAIR_DESTS


def test_single_boolean_flags_unchanged() -> None:
    """AC4: unpaired booleans still send the flag for true and nothing for false."""
    flags = {"flatten": "--no-flatten"}

    assert server._params_to_argv({"trace": False, "no_cache": True}, flags) == ["--no-cache"]
    assert server._params_to_argv({"trace": False, "no_cache": True}) == ["--no-cache"]
