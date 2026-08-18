"""Tests for data models."""

from __future__ import annotations

from pathlib import Path

from booley.harness.models import (
    AgentResult,
    CommandEntry,
    ExecutionContext,
    OnSuccess,
    StepResult,
    TicketContext,
)


class TestTicketContext:
    def test_is_integration_true(self):
        ctx = TicketContext(
            slug="t",
            ticket_path=Path("/t.md"),
            ticket_type="mod",
            branch="int/merge-feature",
            summary="s",
        )
        assert ctx.is_integration is True

    def test_is_integration_false_when_not_int_branch(self):
        ctx = TicketContext(
            slug="t",
            ticket_path=Path("/t.md"),
            ticket_type="mod",
            branch="master",
            summary="s",
        )
        assert ctx.is_integration is False

    def test_logs_dir(self, tmp_path: Path):
        ctx = TicketContext(
            slug="my-ticket",
            ticket_path=Path("/t.md"),
            ticket_type="mod",
            branch="m",
            summary="s",
            project_root=tmp_path,
        )
        assert ctx.logs_dir == tmp_path / ".booley" / "project" / "tickets" / "logs" / "my-ticket"


class TestOnSuccess:
    def test_defaults(self):
        os = OnSuccess()
        assert os.destination == "review"
        assert os.merge is True
        assert os.cleanup is True
        assert os.triage_report is True

    def test_from_dict_none(self):
        os = OnSuccess.from_dict(None)
        assert os.destination == "review"
        assert os.merge is True

    def test_from_dict_partial(self):
        os = OnSuccess.from_dict({"destination": "done"})
        assert os.destination == "done"
        assert os.merge is True
        assert os.cleanup is True
        assert os.triage_report is True

    def test_from_dict_full(self):
        os = OnSuccess.from_dict(
            {
                "destination": "done",
                "merge": False,
                "cleanup": True,
                "triage_report": False,
            }
        )
        assert os.destination == "done"
        assert os.merge is False
        assert os.cleanup is True
        assert os.triage_report is False

    def test_validate_ok(self):
        assert OnSuccess().validate() == []
        assert OnSuccess(destination="done").validate() == []

    def test_validate_bad_destination(self):
        errors = OnSuccess(destination="bogus").validate()
        assert len(errors) == 1
        assert "bogus" in errors[0]

    def test_validate_bad_triage_report(self):
        errors = OnSuccess(triage_report="yes").validate()  # type: ignore[arg-type]
        assert errors == ["on_success.triage_report must be true or false"]


class TestExecutionContext:
    def test_from_dict_roundtrip(self):
        d = {
            "targets": ["config_a", "config_b"],
            "defines": ["PROTECTED_MODE"],
            "testbench_top": "my_module_tb",
            "sim_eda_tool": "verilator-simple-sandbox",
            "synth_eda_tool": "yosys",
            "lint_eda_tool": "verilator",
            "synthesis": {"config_a": {"cmd": "syn config_a", "timeout_ms": 600000}},
            "pass_sentinels": ["[SIM_RESULT] PASSED"],
            "fail_sentinels": ["[SIM_RESULT] FAILED"],
        }
        ec = ExecutionContext.from_dict(d)
        assert ec.targets == ["config_a", "config_b"]
        assert ec.defines == ["PROTECTED_MODE"]
        assert ec.testbench_top == "my_module_tb"
        assert ec.synthesis["config_a"].cmd == "syn config_a"
        rt = ec.to_dict()
        assert rt == d

    def test_from_dict_empty(self):
        ec = ExecutionContext.from_dict({})
        assert ec.targets == []
        assert ec.defines == []
        assert ec.testbench_top == ""
        assert ec.synthesis == {}

    def test_from_dict_no_synthesis(self):
        ec = ExecutionContext.from_dict(
            {
                "targets": ["config_a"],
                "defines": [],
                "testbench_top": "my_tb",
            }
        )
        assert ec.synthesis == {}
        assert ec.targets == ["config_a"]

    def test_synthesis_command_entry(self):
        ec = ExecutionContext(
            targets=["config_a"],
            defines=[],
            testbench_top="tb",
            synthesis={"config_a": CommandEntry(cmd="python syn.py", timeout_ms=300000)},
        )
        assert ec.synthesis["config_a"].cmd == "python syn.py"
        d = ec.to_dict()
        assert d["synthesis"]["config_a"]["cmd"] == "python syn.py"
        assert d["synthesis"]["config_a"]["timeout_ms"] == 300000


class TestAgentResult:
    def test_defaults(self):
        r = AgentResult()
        assert r.output == ""
        assert r.structured is None


class TestStepResult:
    def test_defaults(self):
        r = StepResult()
        assert r.metadata == {}
        assert r.block_reason is None
