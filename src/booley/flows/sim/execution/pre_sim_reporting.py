"""One presentation contract for each actual Pre-Sim Commands firing."""

from __future__ import annotations

from collections.abc import Mapping

from .contract import PreSimEvidence


def pre_sim_document(evidence: PreSimEvidence) -> dict[str, object]:
    """Normalize an execution result without deriving an exit code from status."""
    return {
        "command_count": len(evidence.commands),
        "test_names": list(evidence.test_names),
        "status": evidence.status,
        "returncode": evidence.returncode,
        "elapsed_s": evidence.elapsed_s,
        "detail": evidence.detail,
        "stdout_tail": evidence.stdout_tail,
        "stderr_tail": evidence.stderr_tail,
    }


def pre_sim_report_line(selector: str, evidence: Mapping[str, object]) -> str:
    """Render the same line for durable, legacy, and coverage execution."""
    names = evidence["test_names"]
    label = ",".join(names) if names else "default"
    rc = evidence.get("returncode")
    return (
        f"pre_run_commands ({evidence['command_count']} line(s)) for {selector}/{label}: "
        f"rc={rc if rc is not None else 'unavailable'} in {evidence['elapsed_s']:.1f}s"
    )
