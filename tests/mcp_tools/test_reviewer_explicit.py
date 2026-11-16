"""Explicit discovery review contracts (issues #1507, #4301)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator

from booley.mcp.base import McpToolResult
from booley.mcp.schema_extractor import extract_schema
from booley.mcp.server import _params_to_argv
from booley.specialists.reviewer import ReviewerSpecialist

ISSUE = {
    "severity": "MAJOR",
    "confidence": "HIGH",
    "category": "bugs",
    "file": "rtl/dut.sv",
    "line": 1,
    "summary": "Wrong reset value",
    "kind": "code_defect",
    "disposition": "current",
    "ticket_clause": "Reset clears output",
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/dut.sv").write_text("module dut; endmodule\n")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


def endpoint(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[ReviewerSpecialist, MagicMock]:
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), *args])
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps(payload if payload is not None else {"issues": [ISSUE]}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer, invoke


def discovery(
    root: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[McpToolResult, ReviewerSpecialist, MagicMock]:
    reviewer, invoke = endpoint(
        root,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
        **kwargs,
    )
    return reviewer._run(), reviewer, invoke


@pytest.mark.parametrize("flag", ["--verify", "--waive"])
def test_verify_and_waive_are_unknown_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    argv = ["--work-dir", str(tmp_path), flag, *(["id", "why"] if flag == "--waive" else [])]
    with pytest.raises(SystemExit) as raised:
        ReviewerSpecialist().parse_args(argv)
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name,value", [("verify", True), ("waive", [["id", "why"]])], ids=["verify", "waive"]
)
def test_mcp_schema_refuses_verify_and_waive(name: str, value: object) -> None:
    validator = Draft202012Validator(extract_schema(ReviewerSpecialist()._parser))
    messages = [error.message for error in validator.iter_errors({name: value})]
    assert any("Additional properties are not allowed" in message for message in messages)


@pytest.mark.parametrize("next_focus", ["bugs", "security"])
def test_done_findings_do_not_block_discovery(
    project: Path, monkeypatch: pytest.MonkeyPatch, next_focus: str
) -> None:
    initial, _, _ = discovery(project, monkeypatch)
    assert initial.exit_code == 0 and initial.detail["issue_list"]
    (project / "rtl/dut.sv").write_text("module dut; wire changed; endmodule")
    reviewer, invoke = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", next_focus],
        payload={"issues": []},
    )
    assert reviewer._run().exit_code == 0
    # rtl/bugs and rtl/security each declare a checklist and a hunt guide: one agent call each.
    assert invoke.call_count == 2


def test_mcp_pair_forwarding_is_generic() -> None:
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
    assert "default" not in extract_schema(ReviewerSpecialist()._parser)["properties"]["scope"]
