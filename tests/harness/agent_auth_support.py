"""Shared set-up for the harness tests of the Agent CLI credential policy.

``pin_venue`` fakes the Sandbox venue check, choosing host or Sandbox, ``install_agent_clis`` makes Doctor see both
Agent CLIs, and ``credential_findings`` runs Doctor's agent credential section alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding
from booley.harness import doctor
from booley.runtime.runtime_context import RuntimeLocation
from tests.sandbox_venue_fake import place


def pin_venue(monkeypatch: pytest.MonkeyPatch, *, inside: bool) -> None:
    """Make the process a Sandbox (``inside``) or the host for the auth policy and Codex."""
    place(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX if inside else RuntimeLocation.HOST)


def install_agent_clis(home: Path) -> None:
    """Create ``~/.claude`` and ``~/.codex`` so Doctor finds both Agent CLIs installed."""
    for name in (".claude", ".codex"):
        (home / name).mkdir(parents=True, exist_ok=True)


def credential_findings(root: Path, booley_toml: dict[str, Any]) -> list[DiagnosticFinding]:
    """Run Doctor's agent credential checks for a Project and return its findings."""
    audit = doctor.ProjectAudit(
        project_root=root,
        booley_dir=root / ".booley",
        booley_toml=booley_toml,
        first_target="",
    )
    # private: run_doctor_result runs every host, Docker and Flow probe first; this is its
    # agent credential section alone.
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    return reporter.findings or []
