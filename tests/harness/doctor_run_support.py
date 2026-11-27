"""Whole-run helpers for the Doctor tests that check one section of its report.

``enter_sandbox`` fakes the Sandbox venue check and the Sandbox image contents.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding
from booley.harness import activity_hooks, doctor, web_isolation
from booley.runtime import dashboard_extension
from booley.runtime.runtime_context import RuntimeLocation
from tests.sandbox_venue_fake import place


def run_doctor(root: Path) -> doctor.DoctorRunResult:
    """Run the whole Doctor on a Project the host fixtures set up."""
    args = argparse.Namespace(deep=False, verbose=False, concise=False)
    return doctor.run_doctor_result(args, root, record_clean=False)


def enter_sandbox(
    monkeypatch: pytest.MonkeyPatch, location: RuntimeLocation = RuntimeLocation.ISSUED_SANDBOX
) -> None:
    """Make the host fixtures' Doctor run inside the Sandbox, healthy apart from the test."""
    place(monkeypatch, location)
    # The next three stand in for files the Sandbox Image installs, which a test host lacks.
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: None)
    monkeypatch.setattr(dashboard_extension, "installed_error", lambda: None)


def findings(result: doctor.DoctorRunResult, check_id: str) -> list[DiagnosticFinding]:
    """The result's findings with *check_id*."""
    return [finding for finding in result.findings if finding.check_id == check_id]
