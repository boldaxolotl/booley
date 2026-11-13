"""Flow runtime-reality inspection: is each Flow's EDA binary in the Sandbox?

Takes a frozen :class:`FlowRuntimeInspectionRequest` and returns a
:class:`DiagnosticReport`; the caller (Doctor) only renders it. The owner
knows nothing of Doctor, CLI orchestration or rendering. The FPGA
Implementation Flow's Vivado startup probe is owned by
``booley.flows.fpga.vivado_startup``; the caller routes that Flow there.

Permitted effects: builds a :class:`TargetCatalog` (reads the Project's
``.core`` files) and runs one ``sh -c "command -v <binary>"`` per distinct
binary through the request's Sandbox command builder (30-second timeout; the
builder may start the Project's Sandbox). Inside the Sandbox it only calls
``shutil.which``. Writes nothing; never raises on a failed probe.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.runtime import session_runtime
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import LINT_EDA_TOOL_FAMILIES, FuseSocError

__all__ = ["FlowRuntimeInspectionRequest", "inspect_flow_runtime_reality"]

# EDA tool family (after LINT_EDA_TOOL_FAMILIES) -> executable probed in the Sandbox.
_EDA_TOOL_BINARIES = {
    "verilator": "verilator",
    "icarus": "iverilog",
    "yosys": "yosys",
    "vivado": "vivado",
    "verible": "verible-verilog-lint",
}


@dataclass(frozen=True, slots=True)
class FlowRuntimeInspectionRequest:
    """Inputs of one Flow's runtime probes, shared with the FPGA Vivado owner.

    ``inside_sandbox`` implies ``runtime_available``: the caller derives both
    from one runtime, whose availability is "inside, or a container CLI
    exists". ``sandbox_command`` maps an inner argv to the argv that runs it
    in the Project's Sandbox.
    """

    flow_name: str
    targets: tuple[str, ...]
    project_root: Path
    runtime_available: bool
    inside_sandbox: bool
    sandbox_command: Callable[[list[str]], list[str]]


def inspect_flow_runtime_reality(request: FlowRuntimeInspectionRequest) -> DiagnosticReport:
    """Probe every selected Target's EDA binary in the Sandbox."""
    findings = Findings()
    for binary in _runtime_probe_binaries(request):
        _check_session_binary(request, binary, findings)
    return findings.report()


def _runtime_probe_binaries(request: FlowRuntimeInspectionRequest) -> list[str]:
    """Return the distinct runtime executables required by selected Targets."""
    binaries: list[str] = []
    try:
        catalog = TargetCatalog.build(request.project_root)
    except FuseSocError:
        return binaries
    for target in request.targets:
        try:
            handle = catalog.select(target)
        except FuseSocError:
            continue
        eda_tool = handle.eda_tool or ""
        family = LINT_EDA_TOOL_FAMILIES.get(eda_tool, eda_tool)
        binary = _EDA_TOOL_BINARIES.get(family.lower())
        if binary and binary not in binaries:
            binaries.append(binary)
    return binaries


def _check_session_binary(
    request: FlowRuntimeInspectionRequest, binary: str, findings: Findings
) -> None:
    """PASS/FAIL on *binary* being on the Sandbox PATH."""
    flow_name = request.flow_name
    label = f"{flow_name}: '{binary}' on the Sandbox PATH"
    bake_fix = f"bake {binary} into the Sandbox Image and rebuild (booley init --force)"
    if request.inside_sandbox:
        if shutil.which(binary):
            findings.pass_(label)
        else:
            findings.fail(f"{flow_name}: '{binary}' is not on this container's PATH", bake_fix)
        return
    if not request.runtime_available:
        findings.skip(f"{label} - container runtime unavailable")
        return
    try:
        command = request.sandbox_command(["sh", "-c", f"command -v {binary}"])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (session_runtime.SessionError, subprocess.SubprocessError, FileNotFoundError) as exc:
        findings.fail(
            f"{label} (runtime/probe error: {exc})", "run 'booley init --seed' and retry"
        )
        return
    if result.returncode == 0:
        findings.pass_(label)
    else:
        findings.fail(f"{flow_name}: '{binary}' is not on the issued Sandbox PATH", bake_fix)
