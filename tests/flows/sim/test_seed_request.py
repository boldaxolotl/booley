"""``--seed`` and Test Run selectors on the CLI and the MCP request (ADR 0072)."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import MAX_SEED
from booley.mcp.flow_adapter import flow_schema

_SEED_WITH_SELECTOR = "--seed cannot be combined with a --test that names a seed"


@pytest.mark.parametrize(
    ("argv", "seed", "tests"),
    [
        (["--seed", "1"], 1, None),
        (["--seed", str(MAX_SEED)], MAX_SEED, None),
        (["--seed", "random"], "random", None),
        (["--seed", "7", "--test", "stress+soak"], 7, ("stress+soak",)),
        (
            ["--test", "stress@42", "--test", "stress+soak@x3"],
            None,
            ("stress@42", "stress+soak@x3"),
        ),
    ],
)
def test_cli_accepts_seed_and_selector_forms(argv, seed, tests) -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim", *argv])
    assert request.seed == seed
    assert request.test == tests


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--seed", "5", "--test", "a@3"], _SEED_WITH_SELECTOR),
        (["--seed", "random", "--test", "a@x2"], _SEED_WITH_SELECTOR),
        (["--seed", "5", "--test", "a", "--test", "b+v@9"], _SEED_WITH_SELECTOR),
        (["--test", "a@x1@2"], "has more than one '@'"),
        (["--test", "a@x1001"], "exceeds the limit of 1000"),
        (["--test", "a@0"], "out of range"),
        (["--test", "a+b+c"], "more than one '+'"),
        (["--seed", "0"], "out of range"),
        (["--seed", str(MAX_SEED + 1)], "out of range"),
        (["--seed", "01"], "leading zeros"),
        (["--seed", "-1"], "decimal number without a sign"),
        (["--seed", "any"], "decimal number without a sign"),
        (["--seed", "3", "--mode", "elab-only"], "--seed applies to simulation"),
    ],
)
def test_cli_refuses(argv, message, capsys) -> None:
    with pytest.raises(SystemExit):
        parse_request(SimulateFlow(), ["--target", "sim", *argv])
    assert message in capsys.readouterr().err


def test_cli_resume_refuses_a_seed(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit):
        parse_request(SimulateFlow(), ["--resume-from", str(tmp_path / "m.json"), "--seed", "3"])
    assert "--seed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"seed": 5, "test": ["a@3"]}, _SEED_WITH_SELECTOR),
        ({"seed": "random", "test": ["a@x2"]}, _SEED_WITH_SELECTOR),
        ({"test": ["a@x1@2"]}, "has more than one '@'"),
        ({"test": ["a@x1001"]}, "exceeds the limit of 1000"),
        ({"seed": 0}, "out of range"),
        ({"seed": MAX_SEED + 1}, "out of range"),
        ({"seed": True}, "seed"),
        ({"seed": 1.5}, "seed"),
        ({"seed": "soon"}, "decimal number"),
    ],
)
def test_mcp_dispatch_refuses_the_same_requests(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SimRequest(target="sim", **fields)


def test_typed_request_accepts_integer_and_random_seeds(tmp_path) -> None:
    assert SimRequest(target="sim", seed=MAX_SEED).seed == MAX_SEED
    assert SimRequest(target="sim", seed="random").seed == "random"
    assert SimRequest(target="sim", seed="42").seed == 42
    with pytest.raises(ValueError, match="resume_from cannot be combined with seed"):
        SimRequest(resume_from=tmp_path / "manifest.json", seed=1)


def test_mcp_schema_offers_an_integer_range_or_random() -> None:
    schema = flow_schema(SimulateFlow())
    assert schema["properties"]["seed"]["oneOf"] == [
        {"type": "integer", "minimum": 1, "maximum": MAX_SEED},
        {"type": "string", "enum": ["random"]},
    ]
    validator = Draft202012Validator(schema)
    for accepted in (1, MAX_SEED, "random"):
        assert not list(validator.iter_errors({"target": "sim", "seed": accepted}))
    for refused in (0, MAX_SEED + 1, "5", "any", 1.5, None):
        assert list(validator.iter_errors({"target": "sim", "seed": refused})), refused
