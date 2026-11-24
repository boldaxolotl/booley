"""The Mutation Flow's public surface: CLI flags, MCP tool, endpoint kind, criterion producer.

The Flow itself is imported inside the test bodies, so a tree without it fails each test in
its body rather than at collection.
"""

from __future__ import annotations

import os

import pytest

from booley.harness import booley as booley_cli
from booley.mcp import catalog
from booley.mcp.registry import discover_mcp_tools
from booley.runtime import runtime_context


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


def test_count_rejects_text_that_is_not_auto_or_an_integer(tmp_path, capsys) -> None:
    from booley.flows.mutation.flow import MutationFlow

    for bad_count in ("abc", "many", "0", "-3", "1.5"):
        with pytest.raises(SystemExit) as exit_info:
            MutationFlow().parse_args(
                ["--work-dir", str(tmp_path), "--target", "sim_soc", "--count", bad_count]
            )
        stderr = capsys.readouterr().err
        assert exit_info.value.code == 2, bad_count
        assert "argument --count: must be a positive integer or 'auto'" in stderr, bad_count
        assert "_count_type" not in stderr, bad_count


def test_mcp_definition_is_a_flow_with_the_mutation_schema(monkeypatch) -> None:
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_MCP_DEVELOPER_AGENT"):
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


def test_specialist_command_no_longer_runs_mutation_tester(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)

    monkeypatch.setattr("sys.argv", ["booley", "specialist", "mutation_tester"])
    assert booley_cli.main() == 2
    error = capsys.readouterr().err
    assert "'mutation_tester' is not a specialist" in error
    assert "Use booley flow mutation." in error
    assert "coverage_analyst" in error

    monkeypatch.setattr("sys.argv", ["booley", "specialist", "mutation"])
    assert booley_cli.main() == 2
    assert "Use booley flow mutation." in capsys.readouterr().err


def test_mutation_is_discovered_as_a_flow() -> None:
    kinds = {info.name: info.kind for info in discover_mcp_tools()}
    assert kinds["mutation"] == "flow"
