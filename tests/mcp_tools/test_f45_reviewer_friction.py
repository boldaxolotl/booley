"""Executable reproductions for GitHub #88, finding F-45.

These regressions mock only the nondeterministic model response. Target
metadata, prompt/filter behavior, persistence, freshness, and acceptance use
production code.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from booley.criteria.state import DevelopmentState
from booley.specialists.reviewer import ReviewerSpecialist


def _agent_result(*issues: dict) -> MagicMock:
    result = MagicMock()
    result.output = json.dumps({"issues": list(issues)})
    result.structured = None
    return result


def _issue(category: str, file: str, summary: str) -> dict:
    return {
        "severity": "CRITICAL",
        "confidence": "HIGH",
        "category": category,
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reviewed source must satisfy the current Ticket requirements.",
        "file": file,
        "line": 1,
        "summary": summary,
        "fix_suggestion": "Implement the missing behavior.",
    }


@contextlib.contextmanager
def _passthrough_workspace(params, _access, _category=None):
    yield params, None


def _state(tmp_path: Path, monkeypatch, *criteria: str) -> Path:
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.slug = "f45-repro"
    state.init_criteria(dict.fromkeys(criteria, True))
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOOLEY_SLUG", "f45-repro")
    return state_file


def _run_review(argv: list[str]):
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(argv)
    reviewer.read_state()
    return reviewer._run()


def test_cocotb_target_retains_agent_sim_result_requirement(tmp_path, monkeypatch):
    """Cocotb verdicts come from results.xml, never HDL sentinels."""
    tb = tmp_path / "tb" / "test_uart.py"
    tb.parent.mkdir()
    tb.write_text("import cocotb\n@cocotb.test()\nasync def smoke(dut): assert True\n")
    (tmp_path / "uart.core").write_text(
        "CAPI=2:\nname: acme:uart:uart:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - tb/test_uart.py: {file_type: user, copyto: test_uart.py}\n"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim:\n"
        "    filesets: [tb]\n"
        "    toplevel: uart\n"
        "    flow: sim\n"
        "    flow_options:\n"
        "      tool: verilator\n"
        "      cocotb_module: test_uart\n"
    )
    _state(tmp_path, monkeypatch, "review_tb_quality_done")
    false_finding = _issue(
        "quality", "tb/test_uart.py", "Missing [SIM_RESULT] PASSED/FAILED sentinel"
    )

    with (
        patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\")),
        patch("booley.specialists.specialist.isolated_agent_workspace", _passthrough_workspace),
        patch(
            "booley.specialists.specialist._call_agent_sync",
            return_value=_agent_result(false_finding),
        ),
    ):
        result = _run_review(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                "tb/test_uart.py",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )

    assert result.detail["issues"] == 1, result.report_text
    assert result.criterion_met is True


def test_corrective_findings_do_not_complete_terminal_done_review(tmp_path, monkeypatch):
    """Terminal _done is advisory; a CRITICAL correction must remain open."""
    rtl = tmp_path / "rtl" / "uart.sv"
    rtl.parent.mkdir()
    rtl.write_text("module uart; endmodule\n")
    _state(tmp_path, monkeypatch, "review_rtl_bugs_done")
    corrective = _issue("bugs", "rtl/uart.sv", "Reset behavior violates the Ticket contract")

    with (
        patch("booley.specialists.specialist.isolated_agent_workspace", _passthrough_workspace),
        patch(
            "booley.specialists.specialist._call_agent_sync",
            return_value=_agent_result(corrective),
        ),
    ):
        result = _run_review(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                "rtl/uart.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )

    assert result.criterion_met is True, result.report_text


def test_done_correction_survives_empty_rediscovery(tmp_path, monkeypatch):
    """A stochastic rediscovery miss cannot silently clear corrective work."""
    rtl = tmp_path / "rtl" / "uart.sv"
    rtl.parent.mkdir()
    rtl.write_text("module uart; endmodule\n")
    state_file = _state(tmp_path, monkeypatch, "review_rtl_bugs_done")
    corrective = _issue("bugs", "rtl/uart.sv", "Reset behavior violates the Ticket contract")

    with (
        patch("booley.specialists.specialist.isolated_agent_workspace", _passthrough_workspace),
        patch(
            "booley.specialists.specialist._call_agent_sync",
            side_effect=[_agent_result(corrective), _agent_result()],
        ) as agent,
    ):
        argv = [
            "--work-dir",
            str(tmp_path),
            "--scope",
            "rtl/uart.sv",
            "--category",
            "rtl",
            "--focus",
            "bugs",
        ]
        first = _run_review(argv)
        rtl.write_text("module uart; wire changed; endmodule\n")
        second = _run_review(argv)

    detail = DevelopmentState.load(state_file).criteria["review_rtl_bugs_done"].detail
    assert agent.call_count == 2
    assert first.criterion_met is True
    assert second.criterion_met is True
    assert detail["issue_list"][0]["status"] == "current"

    assert detail["issues"] == 1
    assert detail["CRITICAL"] == 1
    assert detail["gate_passed"] is True
    assert "NO FINDINGS" not in second.report_text


def test_explicit_advisory_done_observation_completes(tmp_path, monkeypatch):
    """An explicit advisory observation is reportable but not corrective."""
    rtl = tmp_path / "rtl" / "uart.sv"
    rtl.parent.mkdir()
    rtl.write_text("module uart; endmodule\n")
    state_file = _state(tmp_path, monkeypatch, "review_rtl_bugs_done")
    advisory = _issue("bugs", "rtl/uart.sv", "Consider a clearer local signal name")
    advisory["severity"] = "MINOR"
    advisory["disposition"] = "advisory"

    with (
        patch("booley.specialists.specialist.isolated_agent_workspace", _passthrough_workspace),
        patch(
            "booley.specialists.specialist._call_agent_sync",
            return_value=_agent_result(advisory),
        ),
    ):
        result = _run_review(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                "rtl/uart.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )

    detail = DevelopmentState.load(state_file).criteria["review_rtl_bugs_done"].detail
    assert result.criterion_met is True
    assert detail["issue_list"][0]["status"] == "advisory"
    assert detail["receipt_id"]


def test_clean_verify_preserves_superseded_observations(tmp_path, monkeypatch):
    """Final rediscovery retains lifecycle evidence for absent observations."""
    rtl = tmp_path / "rtl" / "uart.sv"
    rtl.parent.mkdir()
    rtl.write_text("module uart; endmodule\n")
    state_file = _state(tmp_path, monkeypatch, "review_rtl_bugs_clean")
    corrective = _issue("bugs", "rtl/uart.sv", "Reset behavior violates the Ticket")
    advisory = _issue("bugs", "rtl/uart.sv", "Consider a clearer local signal name")
    advisory["severity"] = "MINOR"
    advisory["disposition"] = "advisory"
    verify_fixed = MagicMock(
        output=json.dumps(
            {
                "findings": [
                    {
                        "index": 1,
                        "status": "FIXED",
                        "evidence": "rtl/uart.sv:1 — reset behavior corrected",
                    }
                ]
            }
        ),
        structured=None,
    )

    with (
        patch("booley.specialists.specialist.isolated_agent_workspace", _passthrough_workspace),
        patch(
            "booley.specialists.specialist._call_agent_sync",
            side_effect=[_agent_result(corrective, advisory), verify_fixed, _agent_result()],
        ) as agent,
    ):
        argv = [
            "--work-dir",
            str(tmp_path),
            "--scope",
            "rtl/uart.sv",
            "--category",
            "rtl",
            "--focus",
            "bugs",
        ]
        first = _run_review(argv)
        rtl.write_text("module uart; wire fixed; endmodule\n")
        second = _run_review(argv)

    detail = DevelopmentState.load(state_file).criteria["review_rtl_bugs_clean"].detail
    assert agent.call_count == 3
    assert first.criterion_met is False
    assert second.criterion_met is True
    assert len(detail["observations"]) == 1
    assert detail["observations"][0]["summary"] == advisory["summary"]
    assert detail["observations"][0]["status"] == "superseded"
