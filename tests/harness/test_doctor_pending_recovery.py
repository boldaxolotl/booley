"""Host Doctor leaves pending Sandbox recovery to the lifecycle commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.harness import doctor
from booley.runtime import issuance_invalidation, session_issuance, session_runtime
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.doctor_run_support import finding_messages, run_doctor, write_project


@pytest.mark.parametrize("pending_for", ["this-project", "another-project"])
def test_host_doctor_leaves_pending_sandbox_recovery_to_lifecycle_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending_for: str
) -> None:
    real_up = session_runtime.up
    booley_dir = write_project(tmp_path)
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    monkeypatch.setattr(doctor.session_runtime, "up", real_up)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    identity = str((tmp_path if pending_for == "this-project" else tmp_path / "other").resolve())
    issuance_invalidation.prepare(identity)
    stamps = session_issuance.stamp_store()
    stamps.write(identity, json.dumps({"marker": True}))

    rows = []
    for _ in range(2):
        result = run_doctor(tmp_path)
        assert issuance_invalidation.pending_invalidations() == (identity,)
        assert stamps.path(identity).read_bytes() == b'{"marker": true}'
        assert not [c for c in calls if c[1:3] in (["container", "rm"], ["network", "rm"])]
        assert (
            len(finding_messages(result, Severity.WARN, check_id="host.sandbox-recovery-pending"))
            == 1
        )
        dry_runs = [f for f in result.findings if "dry-run" in f.message]
        assert dry_runs
        assert {f.severity for f in dry_runs} == {Severity.SKIP}
        assert all(f.message.endswith("recovery is pending on this host") for f in dry_runs)
        assert not [f for f in result.findings if "could not enter the Sandbox" in f.message]
        rows.append([(f.severity, f.message) for f in result.findings])
    assert rows[0] == rows[1]
