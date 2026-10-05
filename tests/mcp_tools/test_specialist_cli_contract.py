"""Public Specialist transport, visibility, and timeout regression contracts."""

import argparse
import asyncio
import json
from unittest.mock import Mock

import pytest

from booley.core.models import AgentResult
from booley.harness import booley as cli
from booley.mcp.application import McpApplication
from booley.mcp.base import McpToolResult
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.registry import McpToolInfo
from booley.runtime import job_slots
from booley.specialists.mutation_tester import MutationTesterSpecialist
from booley.specialists.specialist import Specialist


class ProjectSpecialist(Specialist):
    """Migrated Project subclass consuming the shared seconds accessor."""

    name = "project_review"
    description = "Project review fixture"
    default_timeout = 9
    min_timeout = 0
    verdict = 0

    def _build_prompt(self) -> str:
        return "inspect"

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        return McpToolResult(
            exit_code=self.verdict,
            report_text="inspection complete",
            detail={"duration": self.timeout_seconds()},
        )


def command(argv):
    parser = cli._build_parser()
    return cli._normalize_args(parser, parser.parse_args(argv))


def register_project_specialist(data):
    directory = data / "mcp_tools"
    directory.mkdir()
    (directory / "project_review.py").write_text(
        'class ProjectReview(Specialist):\n    name = "project_review"\n'
        '    description = "Project review fixture"\n'
    )


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(
        "booley.runtime.runtime_context.container_only_error", lambda _command: None
    )
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)


@pytest.mark.parametrize("separator", [[], ["--"]])
@pytest.mark.parametrize("verdict", [0, 1, 2])
def test_dispatch_preserves_arguments_and_exit_without_flow_adapter(
    tmp_path, monkeypatch, separator, verdict
):
    from booley.mcp import registry

    info = McpToolInfo(
        name="project_review", path="fixture.py", description="fixture", kind="specialist"
    )
    monkeypatch.setattr(registry, "discover_mcp_tools", lambda **_kwargs: [info])
    endpoint = Mock()
    endpoint.main.return_value = verdict
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: lambda: endpoint)
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(tmp_path / "ticket.md"))
    assert (
        cli._cmd_specialist(
            command(["specialist", "project_review", *separator, "--instruction", "two words"]),
            tmp_path,
        )
        == verdict
    )
    endpoint.main.assert_called_once_with(["--instruction", "two words"])
    endpoint.configure_flow_execution.assert_not_called()


def test_listing_filters_disabled_and_discovers_project_specialist(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    directory = tmp_path / ".booley_project"
    (directory / "mcp_tools").mkdir(parents=True)
    (directory / "booley.toml").write_text("[specialists.reviewer]\nenabled = false\n")
    (directory / "mcp_tools/project_review.py").write_text(
        'from booley.specialists.specialist import Specialist\nclass ProjectReview(Specialist):\n    name = "project_review"\n    description = "Project fixture"\n'
    )
    assert cli._cmd_specialist(command(["specialist"]), tmp_path) == 0
    output = capsys.readouterr().out
    assert "project_review" in output
    assert "coverage_analyst" in output
    assert "mutation_tester" in output
    assert "reviewer" not in output
    assert "tb_coder" not in output
    assert (
        output.index("coverage_analyst")
        < output.index("mutation_tester")
        < output.index("project_review")
    )


@pytest.mark.parametrize(
    ("route", "name", "suggestion"),
    [
        ("specialist", "simulate", "booley flow sim"),
        ("flow", "reviewer", "booley specialist reviewer"),
    ],
)
def test_wrong_kind_suggests_canonical_command(tmp_path, capsys, route, name, suggestion):
    handler = cli._cmd_specialist if route == "specialist" else cli._cmd_flow
    assert handler(command([route, name]), tmp_path) == 2
    assert suggestion in capsys.readouterr().err


@pytest.mark.parametrize("name", ["unknown", "tb_coder"])
def test_unknown_or_hidden_never_loads(tmp_path, monkeypatch, name, capsys):
    loader = Mock()
    monkeypatch.setattr(cli, "_load_mcp_tool_class", loader)
    assert cli._cmd_specialist(command(["specialist", name]), tmp_path) == 2
    assert "booley specialist" in capsys.readouterr().err
    loader.assert_not_called()


def test_host_refusal_precedes_state_reports_and_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.runtime.runtime_context.container_only_error",
        lambda command: f"use booley session enter -- {command}",
    )
    endpoint = ProjectSpecialist()
    for hook in ["read_state", "_invoke_agent", "write_report"]:
        monkeypatch.setattr(endpoint, hook, Mock(side_effect=AssertionError(hook)))
    result = endpoint.execute_cli(["--work-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "booley session enter -- booley specialist project_review" in result.outcome.report_text
    assert not list(tmp_path.iterdir())


def test_module_gate_rejects_disabled_specialist(tmp_path, sandbox, capsys):
    data = tmp_path / ".booley_project"
    data.mkdir()
    register_project_specialist(data)
    (data / "booley.toml").write_text("[specialists.project_review]\nenabled = false\n")
    endpoint = ProjectSpecialist()
    endpoint.read_state = Mock(side_effect=AssertionError("state loaded"))
    assert endpoint.main(["--work-dir", str(tmp_path)]) == 2
    endpoint.read_state.assert_not_called()
    output = capsys.readouterr()
    assert "is disabled" in output.out + output.err


def test_module_gate_reports_retirement_before_loading_state(tmp_path, sandbox, capsys):
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "booley.toml").write_text("[mcp_tools.project_review]\nenabled = false\n")
    endpoint = ProjectSpecialist()
    endpoint.read_state = Mock(side_effect=AssertionError("state loaded"))

    assert endpoint.main(["--work-dir", str(tmp_path)]) == 2
    endpoint.read_state.assert_not_called()
    output = capsys.readouterr()
    assert "[specialists.*]" in output.out + output.err


def test_reviewer_gate_rejects_misspelled_setting_before_loading_state(tmp_path, sandbox, capsys):
    from booley.specialists.reviewer import ReviewerSpecialist

    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "booley.toml").write_text("[specialists.reveiwer]\nenabled = false\n")
    endpoint = ReviewerSpecialist()
    endpoint.read_state = Mock(side_effect=AssertionError("state loaded"))

    assert (
        endpoint.main(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                ".",
                "--category",
                "rtl",
                "--focus",
                "correctness",
            ]
        )
        == 2
    )
    endpoint.read_state.assert_not_called()
    output = capsys.readouterr()
    assert "reveiwer" in output.out + output.err


@pytest.mark.parametrize("selection", ["override", "checkout_snapshot", "subdirectory"])
def test_disabled_gate_uses_selected_project_config(
    tmp_path, monkeypatch, sandbox, selection, capsys
):
    root = tmp_path / "checkout"
    root.mkdir()
    data = root / ("project_data" if selection == "override" else ".booley_project")
    data.mkdir()
    register_project_specialist(data)
    (data / "booley.toml").write_text("[specialists.project_review]\nenabled = false\n")
    if selection == "override":
        (root / "booley.toml").write_text('[project]\ndir = "project_data"\n')
    elif selection == "checkout_snapshot":
        session_data = tmp_path / "session_data"
        session_data.mkdir()
        (session_data / "booley.toml").write_text("[specialists.project_review]\nenabled = true\n")
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(session_data))
    else:
        root = root / "rtl"
        root.mkdir()
    endpoint = ProjectSpecialist()
    endpoint.read_state = Mock(side_effect=AssertionError("state loaded"))
    assert endpoint.main(["--work-dir", str(root)]) == 2
    endpoint.read_state.assert_not_called()
    output = capsys.readouterr()
    assert "is disabled" in output.out + output.err


def test_listing_resolves_project_directory_override(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    (tmp_path / "booley.toml").write_text('[project]\ndir = "project_data"\n')
    data = tmp_path / "project_data"
    (data / "mcp_tools").mkdir(parents=True)
    (data / "booley.toml").write_text("[specialists.reviewer]\nenabled = false\n")
    (data / "mcp_tools/project_review.py").write_text(
        'from booley.specialists.specialist import Specialist\nclass ProjectReview(Specialist):\n    name = "project_review"\n    description = "Project fixture"\n'
    )
    assert cli._cmd_specialist(command(["specialist"]), tmp_path) == 0
    output = capsys.readouterr().out
    assert "project_review" in output
    assert "reviewer" not in output


@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_positive_milliseconds(value):
    with pytest.raises(SystemExit) as error:
        ProjectSpecialist().parse_args(["--timeout-ms", value])
    assert error.value.code == 2


def test_human_timeout_uses_seconds_and_preserves_no_abbreviations():
    endpoint = ProjectSpecialist()
    endpoint.parse_args(["--timeout", "1001"])
    assert endpoint.args.timeout_ms == 1001000
    with pytest.raises(SystemExit) as error:
        endpoint.parse_args(["--time", "1001"])
    assert error.value.code == 2


def test_project_params_round_up_without_a_floor(tmp_path):
    endpoint = ProjectSpecialist()
    endpoint.parse_args(["--work-dir", str(tmp_path), "--timeout-ms", "1001"])
    assert endpoint._agent_call_params().timeout_seconds == 2
    assert endpoint.args.timeout_ms == 1001
    assert ProjectSpecialist().parse_args([]).timeout_ms == 9000


@pytest.mark.parametrize("milliseconds,expected", [(450000, 3), (1500000, 10)])
def test_mutation_auto_count_uses_converted_seconds(tmp_path, monkeypatch, milliseconds, expected):
    endpoint = MutationTesterSpecialist()
    endpoint._args = argparse.Namespace(count="auto", timeout_ms=milliseconds)
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.compute_source_size_budget",
        lambda *_args: {"formula_count": 100},
    )
    monkeypatch.setattr(endpoint, "emit_progress", Mock())
    assert endpoint._resolve_count([], tmp_path)[0] == expected


@pytest.mark.parametrize("verdict", [0, 1, 2])
def test_real_public_and_module_execution_preserve_reports(
    tmp_path, monkeypatch, sandbox, verdict
):
    """Use real endpoint machinery, with only the model and discovery stubbed."""
    from booley.mcp import registry

    info = McpToolInfo(
        name=ProjectSpecialist.name, path="fixture.py", description="fixture", kind="specialist"
    )
    monkeypatch.setattr(registry, "discover_mcp_tools", lambda **_kwargs: [info])
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: ProjectSpecialist)
    monkeypatch.setattr(ProjectSpecialist, "verdict", verdict)
    calls = []

    def model(_self, params):
        calls.append(params)
        return AgentResult(output="inspection complete")

    monkeypatch.setattr(ProjectSpecialist, "_invoke_agent", model)
    project_data = tmp_path / "durable"
    monkeypatch.setattr(
        "booley.runtime.project_dir.resolve_checkout_project_dir", lambda _root: project_data
    )
    argv = ["--work-dir", str(tmp_path)]
    assert (
        cli._cmd_specialist(command(["specialist", ProjectSpecialist.name, *argv]), tmp_path)
        == verdict
    )
    endpoint = ProjectSpecialist()
    monkeypatch.setattr("sys.argv", ["booley.specialists.project_review", *argv])
    with pytest.raises(SystemExit) as exited:
        endpoint.cli()
    assert exited.value.code == verdict

    invoke_mcp(tmp_path, verdict)
    assert [params.timeout_seconds for params in calls] == [9, 9, 9]
    reports = list((project_data / "mcp-tool-reports").rglob("report.json"))
    assert len(reports) == 3
    payloads = [json.loads(path.read_text()) for path in reports]
    assert all(payload["exit_code"] == verdict for payload in payloads)
    assert all(payload["detail"] == {"duration": 9} for payload in payloads)


@pytest.mark.parametrize(
    "argv", [["booley", "specialist", "reviewer"], ["python", "-m", "booley.specialists.reviewer"]]
)
def test_specialist_admission_labels(argv):
    assert job_slots._argv_label(argv) == "reviewer"


def test_endpoint_help_is_allowed_on_host(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        cli._cmd_specialist(command(["specialist", "reviewer", "--help"]), tmp_path)
    assert error.value.code == 0
    output = capsys.readouterr().out
    for flag in [
        "--project",
        "--report-dir",
        "--diagnostic",
        "--model",
        "--max-turns",
        "--timeout",
    ]:
        assert flag in output
    assert "--work-dir" not in output
    assert "--timeout-ms" not in output


def test_empty_registry_and_load_failure(tmp_path, monkeypatch, capsys):
    from booley.mcp import registry

    monkeypatch.setattr(registry, "discover_mcp_tools", lambda **_kwargs: [])
    assert cli._cmd_specialist(command(["specialist"]), tmp_path) == 0
    assert "none discovered" in capsys.readouterr().out
    info = McpToolInfo(name="broken", path="broken.py", description="", kind="specialist")
    monkeypatch.setattr(registry, "discover_mcp_tools", lambda **_kwargs: [info])

    def broken(_info):
        raise ImportError("missing dependency")

    monkeypatch.setattr(cli, "_load_mcp_tool_class", broken)
    assert cli._cmd_specialist(command(["specialist", "broken"]), tmp_path) == 2
    assert "missing dependency" in capsys.readouterr().err


def test_reviewer_initial_and_verification_calls_round_up(tmp_path, monkeypatch):
    from booley.specialists.reviewer import ReviewerSpecialist

    endpoint = ReviewerSpecialist()
    endpoint.parse_args(
        [
            "--category",
            "rtl",
            "--focus",
            "bugs",
            "--scope",
            "rtl",
            "--work-dir",
            str(tmp_path),
            "--timeout-ms",
            "1001",
        ]
    )
    for hook in [
        "_build_prompt",
        "_build_system_prompt",
        "_build_verify_prompt",
        "_build_verify_system_prompt",
    ]:
        monkeypatch.setattr(endpoint, hook, lambda *_args, **_kwargs: "fixture")
    assert endpoint._single_review_params("bugs").timeout_seconds == 2
    assert endpoint._verify_agent_params("bugs", {}, None).timeout_seconds == 2


@pytest.mark.parametrize("milliseconds,expected", [(1, 1), (1001, 1), (2501, 3)])
def test_mutation_creator_allocates_eighty_percent_before_rounding(
    tmp_path, monkeypatch, milliseconds, expected
):
    endpoint = MutationTesterSpecialist()
    endpoint.parse_args(
        [
            "--target",
            "sim_demo",
            "--scope",
            "rtl",
            "--work-dir",
            str(tmp_path),
            "--timeout-ms",
            str(milliseconds),
        ]
    )
    captured = []

    def model(params):
        captured.append(params)
        return AgentResult(output="")

    monkeypatch.setattr(endpoint, "_invoke_agent_with_resume", model)
    monkeypatch.setattr(endpoint, "_persist_session_id", lambda _key: None)
    endpoint._invoke_creator("fixture", resume=False, attempt=1)
    assert captured[0].timeout_seconds == expected


@pytest.mark.parametrize("route", ["flow", "specialist"])
def test_top_level_invalid_duration_is_rejected(tmp_path, route):
    name = "lint" if route == "flow" else "reviewer"
    args = (
        ["--target", "lint"]
        if route == "flow"
        else ["--category", "rtl", "--focus", "bugs", "--scope", "rtl"]
    )
    handler = cli._cmd_flow if route == "flow" else cli._cmd_specialist
    with pytest.raises(SystemExit) as error:
        handler(command([route, name, *args, "--timeout", "1.5"]), tmp_path)
    assert error.value.code == 2


def test_coverage_provider_preserves_minimum_and_rounding(tmp_path):
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    endpoint = CoverageAnalystSpecialist()
    endpoint.parse_args(["--campaign", "coverage.json", "--timeout-ms", "1001"])
    params = endpoint._coverage_agent_params(
        "fixture", tmp_path, tmp_path / "coverage.json", tmp_path / "audit.json"
    )
    assert params.timeout_seconds == endpoint.min_timeout
    endpoint.args.timeout_ms = endpoint.min_timeout * 1000 + 1
    params = endpoint._coverage_agent_params(
        "fixture", tmp_path, tmp_path / "coverage.json", tmp_path / "audit.json"
    )
    assert params.timeout_seconds == endpoint.min_timeout + 1


def test_builtin_specialist_defaults_stay_seconds_for_watchdogs():
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
    from booley.specialists.reviewer import ReviewerSpecialist
    from booley.specialists.tb_coder import TbCoderSpecialist

    for cls, argv in [
        (ReviewerSpecialist, ["--category", "rtl", "--focus", "bugs", "--scope", "rtl"]),
        (MutationTesterSpecialist, ["--target", "sim_demo", "--scope", "rtl"]),
        (CoverageAnalystSpecialist, ["--campaign", "coverage.json"]),
        (TbCoderSpecialist, ["--instruction-file", "instruction.md", "--scope", "tb"]),
    ]:
        endpoint = cls()
        endpoint.parse_args(argv)
        assert endpoint.args.timeout_ms == endpoint.default_timeout * 1000
        assert endpoint.timeout_seconds() == endpoint.default_timeout
        assert 0 < endpoint.default_timeout < 10000


def test_specialist_full_light_queue_rejects_before_model(tmp_path, monkeypatch, sandbox, capsys):
    import os

    slots = tmp_path / "slots"
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(slots))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    store = job_slots.SlotStore(slots)
    caps = job_slots.SlotCaps()
    for _ in range(caps.max_light + caps.queue_max):
        store.submit(job_slots.CLASS_LIGHT, pid=os.getpid(), argv=[])
    endpoint = ProjectSpecialist()
    monkeypatch.setattr(
        endpoint, "_invoke_agent", Mock(side_effect=AssertionError("model called"))
    )
    assert endpoint.main(["--work-dir", str(tmp_path)]) == 2
    assert "queue is full" in capsys.readouterr().err
    assert (
        len(list((slots / job_slots.CLASS_LIGHT).glob("*.json")))
        == caps.max_light + caps.queue_max
    )


def invoke_mcp(tmp_path, verdict):
    async def dispatch(_name, arguments, _source):
        assert arguments == {"work_dir": str(tmp_path)}
        assert ProjectSpecialist().main(["--work-dir", arguments["work_dir"]]) == verdict
        return []

    application = McpApplication(
        [
            {
                "name": ProjectSpecialist.name,
                "description": "fixture",
                "schema": flow_schema(ProjectSpecialist()),
            }
        ],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _error: None,
    )
    asyncio.run(application.call_tool(ProjectSpecialist.name, {"work_dir": str(tmp_path)}))
