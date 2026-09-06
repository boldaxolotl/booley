"""Contract between the direct CLI and MCP adapters for the lint pilot."""

from __future__ import annotations

import pytest

from booley.flows.lint.flow import LintFlow
from booley.mcp import server as mcp_server
from booley.runtime.endpoint_execution import EXIT_ERROR


@pytest.mark.asyncio
async def test_cli_and_mcp_adapters_return_equivalent_structured_outcomes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both public invocation paths preserve one failed lint verdict."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)

    cli_result = LintFlow().execute_cli(["--target", "missing", "--diagnostic"])

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
    assert report.get("report_text", "") == cli_result.outcome.report_text
    assert report["detail"] == cli_result.outcome.detail
