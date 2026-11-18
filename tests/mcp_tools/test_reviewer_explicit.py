"""Explicit discovery review contracts (issues #1507, #4301)."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from booley.mcp.schema_extractor import extract_schema
from booley.mcp.server import _params_to_argv
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist
from tests.mcp_tools.reviewer_support import ScriptedAgent, install_agent, issue, run_review


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


def discovery(project: Path, focus: str = "bugs") -> EndpointOutcome:
    """Run one rtl discovery review of ``rtl/dut.sv``."""
    argv = ["--work-dir", str(project), "--scope", "rtl/dut.sv", "--category", "rtl"]
    return run_review([*argv, "--focus", focus])[0]


def test_verify_is_an_unknown_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--work-dir", str(tmp_path), "--verify"]
    with pytest.raises(SystemExit) as raised:
        ReviewerSpecialist().parse_args(argv)
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_mcp_schema_refuses_verify() -> None:
    # Private seam: the parser is the accessor `mcp/flow_adapter.py` feeds the schema extractor.
    validator = Draft202012Validator(extract_schema(ReviewerSpecialist()._parser))
    messages = [error.message for error in validator.iter_errors({"verify": True})]
    assert any("Additional properties are not allowed" in message for message in messages)


@pytest.mark.parametrize("next_focus", ["bugs", "security"])
def test_done_findings_do_not_block_discovery(
    project: Path, agent: ScriptedAgent, next_focus: str
) -> None:
    agent.answer({"issues": [issue(line=1, ticket_clause="Reset clears output")]})
    initial = discovery(project)
    assert initial.exit_code == 0 and initial.detail["issue_list"]
    (project / "rtl/dut.sv").write_text("module dut; wire changed; endmodule", encoding="utf-8")
    agent.calls.clear()
    agent.answer({"issues": []})
    assert discovery(project, next_focus).exit_code == 0
    # rtl/bugs and rtl/security each declare a checklist and a hunt guide: one agent call each.
    assert len(agent.calls) == 2


def test_mcp_pair_forwarding_is_generic() -> None:
    # Private seam: the MCP server's argv builder runs only inside the server process.
    assert _params_to_argv({"pair": [["one", "first"], ["two", "second"]]}) == [
        "--pair",
        "one",
        "first",
        "--pair",
        "two",
        "second",
    ]


@pytest.mark.parametrize("flag", ["--sco", "--ste"])
def test_review_arguments_do_not_accept_abbreviations(flag: str) -> None:
    with pytest.raises(SystemExit):
        ReviewerSpecialist().parse_args([flag, "value"])


def test_reviewer_schema_does_not_inject_interactive_scope_default() -> None:
    # Private seam: same parser accessor as the schema test above.
    schema = extract_schema(ReviewerSpecialist()._parser)
    assert "default" not in schema["properties"]["scope"]
