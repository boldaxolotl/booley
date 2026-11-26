"""Behavior tests for the issued-image MCP catalog probe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema.exceptions import SchemaError

from booley.core.booley_layout import BooleyLayoutError
from booley.mcp import server as mcp_server
from tests.subprocess_budget import run_bounded_child


def _definition(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": "Probe fixture",
        "schema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize("writable", [True, False], ids=["writable", "unwritable"])
def test_probe_entrypoint_builds_real_interactive_catalog(tmp_path: Path, writable: bool) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[project]\nname = "probe"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BOOLEY_DIR"] = str(booley_dir)
    env["BOOLEY_MCP_DEVELOPER_AGENT"] = "1"
    env.pop("BOOLEY_LOGS_DIR", None)
    source_root = Path(__file__).parents[2] / "src"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), existing_pythonpath) if part
    )

    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    if not writable:
        booley_dir.chmod(0o555)
    try:
        if not writable and os.access(booley_dir, os.W_OK):
            pytest.skip("directory permissions are not enforced for this user")
        result = run_bounded_child(
            [sys.executable, "-m", "booley.mcp.probe"],
            cwd=tmp_path,
            env=env,
        )
    finally:
        booley_dir.chmod(0o755)

    assert before == sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["errors"] == []
    assert "bwave" in payload["tools"]
    assert payload["logs_dir_ok"] is True
    assert payload["logs_dir_writable"] is writable
    assert Path(payload["logs_dir"]).parent == booley_dir / "job_logs"


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
    # Private: both cases are defects of built-in definitions, which no Project file can
    # change, so discovery is replaced; the live server's only public entry, `main()`,
    # also serves on stdio, so `_build_server` is called directly. Reconciliation would
    # scan a Job store this test does not have.
    monkeypatch.setattr(
        mcp_server,
        "_discover_booley_mcp_tools",
        lambda: (definitions, []),
    )
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)

    with pytest.raises(error_type):
        mcp_server.build_mcp_probe_payload()
    with pytest.raises(error_type):
        mcp_server._build_server()


def test_mcp_probe_accepts_relocated_job_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Logs under the checkout's relocated Booley Directory are ok; the default is not."""
    from booley.config.agent import set_agent_settings
    from booley.runtime.booley_dir import reset_cache

    (tmp_path / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    # The probe loads this Project's [agent] settings process-wide; undo it even on failure.
    request.addfinalizer(lambda: set_agent_settings(None))
    request.addfinalizer(reset_cache)

    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "tools/booley/job_logs/abc"))
    assert mcp_server.build_mcp_probe_payload()["logs_dir_ok"] is True

    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / ".booley/job_logs/abc"))
    assert mcp_server.build_mcp_probe_payload()["logs_dir_ok"] is False

    # A retired layout in the working directory reads as "not ok", never a traceback. A real
    # retired layout also stops the catalog build, so only the layout lookup is replaced.
    monkeypatch.setattr(
        mcp_server,
        "own_booley_dir_repo_path",
        Mock(side_effect=BooleyLayoutError("retired layout")),
    )
    assert mcp_server.build_mcp_probe_payload()["logs_dir_ok"] is False
