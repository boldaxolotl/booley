"""Behavior tests for the issued-image MCP catalog probe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema.exceptions import SchemaError

from booley.mcp import server as mcp_server


def _definition(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": "Probe fixture",
        "schema": {"type": "object", "properties": {}},
    }


def test_probe_entrypoint_builds_real_interactive_catalog(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(
        '[project]\nname = "probe"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project_dir)
    env["BOOLEY_MCP_MODE"] = "interactive"
    env.pop("BOOLEY_LOGS_DIR", None)
    source_root = Path(__file__).parents[2] / "src"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), existing_pythonpath) if part
    )

    result = subprocess.run(
        [sys.executable, "-m", "booley.mcp.probe"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["errors"] == []
    assert "bwave" in payload["tools"]
    assert payload["logs_dir_ok"] is True


@pytest.mark.parametrize(
    ("definitions", "error_type"),
    [
        ([_definition("synth"), _definition("asic_synthesize")], ValueError),
        (
            [
                {
                    **_definition("broken"),
                    "schema": {"type": "not-a-json-schema-type"},
                }
            ],
            SchemaError,
        ),
    ],
    ids=["canonical-name-collision", "invalid-schema"],
)
def test_probe_and_live_server_share_catalog_validation(
    monkeypatch: pytest.MonkeyPatch,
    definitions: list[dict[str, object]],
    error_type: type[Exception],
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_discover_booley_mcp_tools",
        lambda: (definitions, []),
    )
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_locks", lambda: None)
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)

    with pytest.raises(error_type):
        mcp_server.build_mcp_probe_payload()
    with pytest.raises(error_type):
        mcp_server._build_server()
