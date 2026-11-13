"""Interactive Mode MCP server probe: the one owner of its rules (ADR 0018).

Runs ``python -m booley.mcp.probe`` once in the Sandbox image and returns the
complete observations as a :class:`DiagnosticReport`: the probe command and its
environment, the 60-second bound, start-failure, exit and JSON grading with the
always-shown output excerpt, payload validation, interactive log setup, required
endpoints (B-Wave, enabled Simulation/Lint/Synthesis Flows, FPGA when the caller
covers it), advisory endpoints with their explicit opt-outs, and the verbose-only
tool list.

Permitted effects: one ``docker run --init --rm`` of the probe with the Project
Root mounted at ``/work`` (60-second timeout). The probe may create its
interactive log directory under the Booley Directory. The owner prints nothing
and never raises on a failed probe; the caller renders the report.

The caller decides whether the probe is worth running (container runtime and
image present) and what the optional FPGA axis covers; this module holds the
rest of the policy.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.audit.output_excerpt import output_excerpt_lines
from booley.config.flow_enablement import flow_enabled_from_config
from booley.core.boundary import is_str_list
from booley.runtime.booley_dir import sandbox_booley_dir
from booley.runtime.platform_paths import docker_mount_path, run_captured_bounded
from booley.targets.flow_names import config_section

__all__ = ["McpServerInspectionRequest", "inspect_mcp_server"]

_ADVISORY_CHECK_ID = "interactive.mcp-tool-advisory-missing"
_REBUILD_FIX = "rebuild sandbox image"
# B-Wave is unconditionally required; enabled deterministic Flows are added below.
_BASE_REQUIRED_MCP_TOOLS = frozenset({"bwave"})
# Deterministic Flows whose endpoint is required while the Flow is enabled. Doctor
# and readiness keep their own tuples: they encode different policies.
_REQUIRED_FLOW_ENDPOINTS = ("sim", "lint", "synth")
# MCP tools worth a heads-up when expected-but-absent from the MCP surface. An explicit
# ``[specialists.<name>].enabled = false`` (or ``[flows.<name>].enabled = false``) opts out.
_ADVISORY_SPECIALIST_TOOLS = frozenset({"reviewer"})
_ADVISORY_FLOW_TOOLS = frozenset({"mutation"})
_MCP_PROBE_TIMEOUT_S = 60
_EXCERPT_LINES = 8


@dataclass(frozen=True, slots=True)
class McpServerInspectionRequest:
    """What the probe needs to run and be graded against the Project's configuration."""

    project_root: Path  # Project Root, mounted at /work
    booley_dir: Path  # host Booley Directory (probe env BOOLEY_DIR)
    docker_exe: str  # container CLI
    image: str  # Sandbox image to run the probe in
    booley_toml: Mapping[str, Any]  # parsed Project configuration
    fpga_covered: bool  # whether the caller covers the optional FPGA axis


def inspect_mcp_server(request: McpServerInspectionRequest) -> DiagnosticReport:
    """Run the in-Sandbox MCP probe and grade its payload."""
    findings = Findings()
    env = os.environ.copy()
    env["BOOLEY_DIR"] = str(request.booley_dir)
    env["BOOLEY_MCP_MODE"] = "interactive"
    try:
        result = run_captured_bounded(
            _probe_command(request),
            timeout=_MCP_PROBE_TIMEOUT_S,
            cwd=request.project_root,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        findings.fail(f"MCP server probe failed to start: {exc}", _REBUILD_FIX)
        return findings.report()
    _grade_probe_result(request, result, findings)
    return findings.report()


def _probe_command(request: McpServerInspectionRequest) -> list[str]:
    return [
        request.docker_exe,
        "run",
        "--init",
        "--rm",
        "-v",
        f"{docker_mount_path(request.project_root)}:/work",
        "-w",
        "/work",
        "-e",
        f"BOOLEY_DIR={sandbox_booley_dir(request.project_root)}",
        "-e",
        "BOOLEY_MCP_MODE=interactive",
        request.image,
        "python",
        "-m",
        "booley.mcp.probe",
    ]


def _grade_probe_result(
    request: McpServerInspectionRequest,
    result: subprocess.CompletedProcess[str],
    findings: Findings,
) -> None:
    if result.returncode != 0:
        findings.fail(f"MCP server probe failed with exit {result.returncode}", _REBUILD_FIX)
        _add_output_excerpt(findings, result)
        return
    payload = _parse_probe_stdout(result.stdout)
    if payload is None:
        findings.fail("MCP server probe did not return valid JSON", _REBUILD_FIX)
        _add_output_excerpt(findings, result)
        return
    _grade_payload(request, payload, findings)


def _add_output_excerpt(findings: Findings, result: subprocess.CompletedProcess[str]) -> None:
    """Attach the informative output lines to the failure, shown whatever the verbosity."""
    for line in output_excerpt_lines(result.stderr or result.stdout or "", _EXCERPT_LINES):
        findings.detail(line, verbose_only=False)


def _parse_probe_stdout(stdout: str) -> dict[str, Any] | None:
    """Return the JSON object on the last non-blank line, or None."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _grade_payload(
    request: McpServerInspectionRequest, payload: dict[str, Any], findings: Findings
) -> None:
    raw_tools = payload.get("tools")
    raw_errors = payload.get("errors", [])
    if not is_str_list(raw_tools) or not is_str_list(raw_errors):
        findings.fail("MCP server probe returned malformed payload", _REBUILD_FIX)
        return
    # ``is_str_list`` validated both; it is a plain predicate, so state the type here.
    mcp_tools = cast("list[str]", raw_tools)
    errors = cast("list[str]", raw_errors)
    if errors:
        findings.fail(f"MCP tool discovery reported {len(errors)} error(s)", errors[0])
        return
    if payload.get("logs_dir_ok") is not True:
        findings.fail("MCP interactive log setup failed", "check .booley mount and permissions")
        return
    missing = _required_tools(request) - set(mcp_tools)
    if missing:
        findings.fail(
            f"MCP missing required endpoint(s): {', '.join(sorted(missing))}",
            "fix [flows.<name>].enabled or reinstall Booley",
        )
        return
    findings.pass_(f"MCP server exposes {len(mcp_tools)} MCP tool(s)")
    advisory_missing = _advisory_tools(request) - set(mcp_tools)
    if advisory_missing:
        findings.warn(
            "MCP missing documented interactive endpoint(s): "
            f"{', '.join(sorted(advisory_missing))}",
            check_id=_ADVISORY_CHECK_ID,
        )
    findings.detail(f"    {', '.join(mcp_tools)}")


def _required_tools(request: McpServerInspectionRequest) -> set[str]:
    required = set(_BASE_REQUIRED_MCP_TOOLS)
    for flow_name in _REQUIRED_FLOW_ENDPOINTS:
        if flow_enabled_from_config(flow_name, request.booley_toml):
            required.add(flow_name)
    if request.fpga_covered and flow_enabled_from_config("fpga", request.booley_toml):
        required.add("fpga")
    return required


def _advisory_tools(request: McpServerInspectionRequest) -> set[str]:
    """Advisory interactive MCP tools that have not been explicitly disabled."""
    specialists_cfg = request.booley_toml.get("specialists", {})
    if not isinstance(specialists_cfg, dict):
        specialists_cfg = {}
    tools = {
        name
        for name in _ADVISORY_SPECIALIST_TOOLS
        if config_section(specialists_cfg, name).get("enabled") is not False
    }
    tools.update(
        name
        for name in _ADVISORY_FLOW_TOOLS
        if flow_enabled_from_config(name, request.booley_toml)
    )
    return tools
