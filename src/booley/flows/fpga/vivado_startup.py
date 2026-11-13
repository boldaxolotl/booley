"""FPGA Flow's Vivado batch-startup probe.

Takes the Flow runtime request shared with ``booley.flows.runtime_reality`` and
returns a :class:`DiagnosticReport`; the caller (Doctor) only renders it. The
owner knows nothing of Doctor, CLI orchestration or rendering.

Permitted effects: one bounded ``sh -c`` through the request's Sandbox command
builder (which may start the Project's Sandbox) that starts ``vivado -mode
batch`` on a Tcl script and requires this attempt's marker. The outer timeout is
30 s; the inner ``timeout`` is 27 s plus a 2 s kill-after and kills the tool's
whole process group. The Tcl script and Vivado's side files live in a
Sandbox-local temporary directory the script removes on exit. Never raises on a
failed probe.
"""

from __future__ import annotations

import re
import secrets
import subprocess

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.flows.runtime_reality import FlowRuntimeInspectionRequest
from booley.runtime import session_runtime

__all__ = ["inspect_vivado_startup"]


def inspect_vivado_startup(request: FlowRuntimeInspectionRequest) -> DiagnosticReport:
    """Require this attempt's Tcl marker from a bounded actual batch startup."""
    findings = Findings()
    label = "fpga: Vivado batch startup"
    if not request.runtime_available:
        findings.skip(f"{label} - container runtime unavailable")
        return findings.report()
    marker = "BOOLEY_VIVADO_STARTUP_" + secrets.token_hex(16)
    try:
        result = subprocess.run(
            request.sandbox_command(_startup_command(marker)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        findings.fail(
            f"{label}: runtime did not complete within 30s",
            "check Sandbox transport and Vivado startup",
        )
        return findings.report()
    except (session_runtime.SessionError, subprocess.SubprocessError, OSError) as exc:
        findings.fail(
            f"{label}: runtime/probe error: {str(exc)[:500]}", "run 'booley init --seed' and retry"
        )
        return findings.report()
    if result.returncode == 0 and marker in result.stdout.splitlines():
        findings.pass_(label)
        return findings.report()
    if result.returncode in (124, 137):
        cause = "Vivado did not start within 27s"
    else:
        lines = (result.stdout + "\n" + result.stderr).splitlines()
        cause = next(
            (
                line.strip()[:500]
                for line in lines
                if re.search(
                    r"error|fatal|failed|cannot|missing|requires|not found", line, re.IGNORECASE
                )
            ),
            f"current Tcl startup marker missing (exit {result.returncode})",
        )
    findings.fail(f"{label}: {cause}", "verify Vivado runtime dependencies in the Sandbox Image")
    return findings.report()


def _startup_command(marker: str) -> list[str]:
    """Keep the Tcl probe, side files and tool process group Sandbox-local."""
    script = (
        "command -v timeout >/dev/null 2>&1 || { echo 'Vivado startup probe requires GNU timeout'; exit 127; }; "
        "probe_dir=$(mktemp -d) || exit 1; "
        "trap 'rm -rf -- \"$probe_dir\"' EXIT HUP INT TERM; "
        'cd "$probe_dir" || exit 1; '
        f"printf '%s\\n' 'puts {marker}' 'exit 0' > startup.tcl; "
        "timeout --kill-after=2s 27s vivado -mode batch -nolog -nojournal -source startup.tcl"
    )
    return ["sh", "-c", script]
