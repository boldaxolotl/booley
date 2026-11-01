"""Contract between the direct CLI and MCP adapters for the lint pilot."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.flows.lint.flow import LintFlow
from booley.mcp import server as mcp_server
from booley.runtime.endpoint_execution import EXIT_ERROR
from tests.python_command_support import python_path_entry


@pytest.mark.asyncio
async def test_cli_and_mcp_adapters_return_equivalent_structured_outcomes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown-Target diagnostic parity with an explicit Project-directory stub."""
    project = tmp_path / ".booley"
    project.mkdir()
    (tmp_path / "booley.toml").write_text('[project]\ndir=".booley"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(project))
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((python_path_entry(tmp_path / "python-shim"), os.environ["PATH"])),
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[3] / "src"))
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)

    cli_result = LintFlow().execute_cli(["--target", "missing", "--diagnostic"])

    diagnostic = "lint Target selection failed: Unknown target 'missing'; selectable Targets: (none authored)"
    assert cli_result.outcome.report_text == diagnostic
    reports = list((project / "flow-reports" / "lint").glob("*/report.json"))
    assert len(reports) == 1
    published = json.loads(reports[0].read_text())
    assert published["report_text"] == diagnostic
    assert published["exit_code"] == EXIT_ERROR

    definition = {
        "name": "lint",
        "description": "Run lint",
        "schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "diagnostic": {"type": "boolean"},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        "module": "flow",
        "module_path": "booley.flows.lint.flow",
        "is_custom": False,
        "is_specialist": False,
        "default_timeout": 60,
    }
    application = mcp_server._build_mcp_application(
        [definition],
        [],
        mcp_server._McpLifetime(None, None),
    )
    mcp_result = await application.call_tool("lint", {"target": "missing", "diagnostic": True})

    assert cli_result.exit_code == EXIT_ERROR
    assert mcp_result.structured_content is not None
    report = mcp_result.structured_content["reports"][0]
    assert report["flow"] == "lint"
    assert report["exit_code"] == cli_result.outcome.exit_code
    assert report["report_text"] == diagnostic
    assert report["report_text"] == cli_result.outcome.report_text
    assert report["detail"] == cli_result.outcome.detail
