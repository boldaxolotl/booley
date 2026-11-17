"""Executable reproductions for GitHub #88, finding F-45.

These regressions script only the nondeterministic model response. Target
metadata, prompt/filter behavior, persistence, freshness, and acceptance use
production code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.runtime.booley_dir import reset_cache
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.mcp_tools.reviewer_support import ScriptedAgent, install_agent, issue, run_review


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Booley project with a one-file RTL core and ``rtl/uart.sv``."""
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text('[project]\nname="f45"\n', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    (tmp_path / "uart.core").write_text(
        "CAPI=2:\nname: ::f45:0\nfilesets:\n  rtl: {files: [rtl/uart.sv]}\n", encoding="utf-8"
    )
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/uart.sv").write_text("module uart; endmodule\n", encoding="utf-8")
    return tmp_path


def finding(category: str, file: str, summary: str, **overrides: Any) -> dict[str, Any]:
    """A CRITICAL current finding."""
    row = {"severity": "CRITICAL", "category": category, "file": file, "line": 1}
    return issue(**{**row, "summary": summary, **overrides})


def review(
    project: Path, scope: str = "rtl/uart.sv", category: str = "rtl", focus: str = "bugs"
) -> EndpointOutcome:
    argv = ["--work-dir", str(project), "--scope", scope, "--category", category]
    return run_review([*argv, "--focus", focus])[0]


def test_cocotb_target_retains_agent_sim_result_requirement(
    project: Path, agent: ScriptedAgent
) -> None:
    """Cocotb verdicts come from results.xml, never HDL sentinels."""
    (project / "tb").mkdir()
    (project / "tb/test_uart.py").write_text(
        "import cocotb\n@cocotb.test()\nasync def smoke(dut): assert True\n", encoding="utf-8"
    )
    (project / "uart.core").write_text(
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
        "      cocotb_module: test_uart\n",
        encoding="utf-8",
    )
    false_finding = finding(
        "quality", "tb/test_uart.py", "Missing [SIM_RESULT] PASSED/FAILED sentinel"
    )
    agent.answer({"issues": [false_finding]})

    result = review(project, "tb/test_uart.py", "tb", "quality")

    assert result.detail["issues"] == 1, result.report_text


def test_corrective_findings_do_not_complete_terminal_done_review(
    project: Path, agent: ScriptedAgent
) -> None:
    """Terminal _done is advisory; a CRITICAL correction must remain open."""
    summary = "Reset behavior violates the Ticket contract"
    agent.answer({"issues": [finding("bugs", "rtl/uart.sv", summary)]})

    result = review(project)

    assert result.exit_code == 0, result.report_text


def test_interactive_discovery_replaces_done_findings(project: Path, agent: ScriptedAgent) -> None:
    """A Sandbox MCP server explicitly replaces findings on each discovery call."""
    summary = "Reset behavior violates the Ticket contract"
    agent.answer([{"issues": [finding("code_style", "rtl/uart.sv", summary)]}, {"issues": []}])

    review(project, focus="code_style")
    (project / "rtl/uart.sv").write_text(
        "module uart; wire changed; endmodule\n", encoding="utf-8"
    )
    second = review(project, focus="code_style")

    detail = second.detail
    assert len(agent.calls) == 2
    assert detail["issue_list"] == []
    assert detail["issues"] == 0
    assert detail["CRITICAL"] == 0
    assert detail["gate_passed"] is True
    assert "NO FINDINGS" in second.report_text


def test_explicit_advisory_done_observation_completes(project: Path, agent: ScriptedAgent) -> None:
    """An explicit advisory observation is reportable but not corrective."""
    advisory = finding(
        "bugs",
        "rtl/uart.sv",
        "Consider a clearer local signal name",
        severity="MINOR",
        disposition="advisory",
    )
    agent.answer({"issues": [advisory]})

    result = review(project)

    assert result.detail["issue_list"][0]["status"] == "advisory"


def test_source_edit_has_one_freshness_verdict(project: Path, agent: ScriptedAgent) -> None:
    """A source edit forces a second real review."""
    review(project, focus="code_style")
    (project / "rtl/uart.sv").write_text(
        "module uart; wire changed; endmodule\n", encoding="utf-8"
    )
    second = review(project, focus="code_style")

    assert len(agent.calls) == 2
    assert "current source" not in second.report_text
