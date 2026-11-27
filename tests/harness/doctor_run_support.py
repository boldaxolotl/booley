"""Whole-run helpers for the Doctor tests that check one section of its report."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding
from booley.harness import doctor, web_isolation
from booley.runtime import runtime_context


def run_doctor(root: Path) -> doctor.DoctorRunResult:
    """Run the whole Doctor on a Project the host fixtures set up."""
    args = argparse.Namespace(deep=False, verbose=False, concise=False)
    return doctor.run_doctor_result(args, root, record_clean=False)


def enter_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the host fixtures' Doctor run inside the Sandbox, healthy apart from the test."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    # The host fixtures pin this false; on a CI runner `/.dockerenv` would decide.
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    # The next two stand in for files the Sandbox Image installs, which a test host lacks.
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)


def findings(result: doctor.DoctorRunResult, check_id: str) -> list[DiagnosticFinding]:
    """The result's findings with *check_id*."""
    return [finding for finding in result.findings if finding.check_id == check_id]
