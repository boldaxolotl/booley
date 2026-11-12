"""The Mutation Flow's public surface: CLI flags, MCP tool, endpoint kind, criterion producer.

The Flow itself is imported inside the test bodies, so a tree without it fails each test in
its body rather than at collection.
"""

from __future__ import annotations

import argparse
import os

import pytest

from booley.harness import booley as booley_cli
from booley.mcp import catalog
from booley.mcp.registry import criterion_endpoint_relationships, discover_mcp_tools
from booley.runtime.endpoint_execution import EXIT_ERROR


def test_cli_parses_every_mutation_flag(tmp_path) -> None:
    from booley.flows.mutation.flow import MutationFlow
    from booley.flows.mutation.request import MutationRequest

    request = MutationFlow().parse_args(
        [
            "--work-dir",
            str(tmp_path),
            "--target",
            "sim_soc",
            "--scope",
            "rtl/a.sv,rtl/b.sv",
            "--count",
            "auto",
            "--seed",
            "3",
            "--min-detected",
            "2",
            "--dry-run",
        ]
    )

    assert isinstance(request, MutationRequest)
    assert (request.target, request.scope, request.count) == (
        "sim_soc",
        "rtl/a.sv,rtl/b.sv",
        "auto",
    )
    assert (request.seed, request.min_detected, request.dry_run) == (3, 2, True)


def test_cli_defaults_leave_count_scope_and_threshold_to_the_run(tmp_path) -> None:
    from booley.flows.mutation.flow import MutationFlow

    request = MutationFlow().parse_args(["--work-dir", str(tmp_path), "--target", "sim_soc"])

    assert (request.scope, request.count, request.min_detected, request.seed) == (
        None,
        None,
        None,
        0,
    )


@pytest.mark.parametrize(
    ("count", "seed", "message"),
    [(0, 0, "--count must be positive"), (2, -1, "--seed must be a non-negative integer")],
)
def test_invalid_values_end_the_run_with_exit_two(
    tmp_path, monkeypatch, count, seed, message
) -> None:
    from tests.flows.mutation.test_flow import (
        _SCOPE,
        _make_endpoint,
        _prepare_scope_files,
        _run_count_preview,
    )

    _prepare_scope_files(tmp_path, [_SCOPE])
    flow = _make_endpoint(tmp_path, monkeypatch, count=count, seed=seed, extra_args=["--dry-run"])

    result, _ = _run_count_preview(flow, monkeypatch)

    assert result.exit_code == EXIT_ERROR
    assert message in result.report_text


def test_count_rejects_text_that_is_not_auto_or_an_integer(tmp_path) -> None:
    from booley.flows.mutation.flow import MutationFlow

    with pytest.raises(SystemExit):
        MutationFlow().parse_args(
            ["--work-dir", str(tmp_path), "--target", "sim_soc", "--count", "many"]
        )


def test_mcp_definition_is_a_flow_with_the_mutation_schema(monkeypatch) -> None:
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_MCP_MODE"):
        monkeypatch.delenv(name, raising=False)
    definitions, errors = catalog.discover_builtin_definitions(
        discover_mcp_tools(), catalog.Exposure.from_mapping(os.environ)
    )
    assert errors == []

    definition = next(item for item in definitions if item["name"] == "mutation")

    assert definition["is_flow"] is True
    assert definition["is_specialist"] is False
    assert definition["default_timeout"] == 1800
    properties = definition["schema"]["properties"]
    assert {"target", "scope", "count", "min_detected", "seed", "dry_run"} <= set(properties)
    assert properties["count"]["type"] == "string"
    assert properties["seed"]["type"] == "integer"
    assert {"instruction", "transcript_dir", "max_turns"}.isdisjoint(properties)


def test_specialist_command_no_longer_runs_mutation_tester(capsys) -> None:
    endpoints = discover_mcp_tools()
    args = argparse.Namespace(
        command="specialist", endpoint_name="mutation_tester", endpoint_args=[]
    )

    assert booley_cli._select_endpoint(args, endpoints, "specialist") == 2
    error = capsys.readouterr().err
    assert "'mutation_tester' is not a specialist" in error
    assert "coverage_analyst" in error
    assert not any(row.name == "mutation_tester" for row in endpoints)

    flow_args = argparse.Namespace(
        command="specialist", endpoint_name="mutation", endpoint_args=[]
    )
    assert booley_cli._select_endpoint(flow_args, endpoints, "specialist") == 2
    assert "Use booley flow mutation." in capsys.readouterr().err


def test_mutation_score_is_produced_by_the_mutation_flow() -> None:
    relationships = criterion_endpoint_relationships(discover_mcp_tools())

    producers = [row.command for row in relationships if "mutation_score" in row.satisfies]
    assert producers == ["mutation"]
    kinds = {info.name: info.kind for info in discover_mcp_tools()}
    assert kinds["mutation"] == "flow"
