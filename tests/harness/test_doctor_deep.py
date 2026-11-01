"""Deep currency depends only on the latest completed attempt, version, and active image."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.harness import auto_doctor, doctor
from booley.harness import doctor_deep as deep
from booley.runtime import doctor_stamp, health

IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64
IMAGE_ENV = "BOOLEY_SANDBOX_IMAGE_ID"


def _record(outcome=deep.DeepOutcome.SUCCESS, *, version=None, image=IMAGE, reasons=()):
    return deep.DeepRecord(
        "2000-01-01T00:00:00Z", version or booley.__version__, image, outcome, reasons
    )


def _codes(status):
    return [reason.code for reason in status.reasons]


def _observe_in(tmp_path, monkeypatch):
    """Observe currency the way an in-Sandbox Doctor does, with the matching image."""
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    return doctor.observe_deep_status(tmp_path)


def _report(booley_dir, findings, summary=None):
    """Persist one attempt through the real Doctor reporting path."""
    summary = summary or deep.DeepAttemptSummary((), (), IMAGE)
    result = doctor.DoctorRunResult(
        {}, findings, int(any(f.severity == Severity.FAIL for f in findings))
    )
    return doctor._report_deep_currency(
        booley_dir, SimpleNamespace(booley_dir=booley_dir), result, summary, True
    )


def test_matching_old_success_never_expires():
    status = deep.evaluate_deep_status(_record(), version=booley.__version__, image_id=IMAGE)
    assert status.current
    assert "01 JAN 2000" in status.render()


def test_all_applicable_reasons_are_reported():
    status = deep.evaluate_deep_status(
        _record(deep.DeepOutcome.FAILED, version="old"), version="new", image_id=OTHER_IMAGE
    )
    assert _codes(status) == ["version-changed", "image-changed", "latest-attempt-failed"]


def test_no_run_and_unverifiable_image_are_both_reported():
    status = deep.evaluate_deep_status(None, version="new", image_id=None)
    assert _codes(status) == ["no-qualifying-run", "image-identity-unavailable"]


def test_completed_attempt_replaces_one_schema2_record(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    _report(booley_dir, ())
    first = json.loads(deep.deep_stamp_path(booley_dir).read_text())
    assert set(first) == {
        "schema_version",
        "completed_at",
        "booley_version",
        "image_id",
        "outcome",
        "reasons",
    }
    assert first["schema_version"] == 2 and first["outcome"] == "success"
    _report(booley_dir, (DiagnosticFinding(Severity.FAIL, "bad", check_id="sim.bad"),))
    second = json.loads(deep.deep_stamp_path(booley_dir).read_text())
    assert (second["outcome"], second["reasons"]) == ("failed", ["sim.bad"])
    assert set(second) == set(first)
    # One file only: no lock, no merge sidecar, no temporary left behind.
    assert list(deep.deep_stamp_path(booley_dir).parent.iterdir()) == [
        deep.deep_stamp_path(booley_dir)
    ]


def test_failed_attempt_after_success_is_due_until_next_success(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    failure = DiagnosticFinding(Severity.FAIL, "bad")
    assert _report(booley_dir, ()).deep_status.current
    failed = _report(booley_dir, (failure,)).deep_status
    assert _codes(failed) == ["latest-attempt-failed"]
    assert json.loads(deep.deep_stamp_path(booley_dir).read_text())["outcome"] == "failed"
    assert _report(booley_dir, ()).deep_status.current
    assert json.loads(deep.deep_stamp_path(booley_dir).read_text())["outcome"] == "success"


@pytest.mark.parametrize(
    "findings,missing,detail",
    [
        pytest.param(
            [DiagnosticFinding(Severity.WARN, "w", check_id=f"warning-{i}") for i in range(10)],
            (),
            " (" + ", ".join(f"warning-{i}" for i in range(8)) + "; +2 more)",
            id="warning-only",
        ),
        pytest.param(
            [
                DiagnosticFinding(Severity.FAIL, "f", check_id="sim.bad"),
                DiagnosticFinding(Severity.WARN, "w", check_id="git.worktree-portability"),
            ],
            ("sim.selftest.bad",),
            " (git.worktree-portability, sim.bad, sim.selftest.bad)",
            id="with-failure",
        ),
        pytest.param(
            [
                DiagnosticFinding(Severity.FAIL, "f"),
                DiagnosticFinding(Severity.WARN, "w", check_id="git.worktree-portability"),
            ],
            (),
            " (git.worktree-portability, unidentified-failure)",
            id="unidentified-failure",
        ),
    ],
)
def test_failed_attempt_names_first_eight_blocking_checks(
    tmp_path, monkeypatch, findings, missing, detail
):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    status = _report(
        booley_dir, tuple(findings), deep.DeepAttemptSummary(missing, (), IMAGE)
    ).deep_status
    assert [(r.code, r.message) for r in status.reasons] == [
        ("latest-attempt-failed", "latest deep attempt failed" + detail)
    ]
    assert status.render() == "deep Doctor due: latest deep attempt failed" + detail


def _schema1_document():
    """A well-formed qualifying record in the two-slot layout schema 1 used."""
    attempt = {
        "completed_at": "2000-01-01T00:00:00Z",
        "completion_order_" + "ns": 1,
        "attempt_" + "id": "1",
        "booley_version": booley.__version__,
        "image_id": IMAGE,
        "outcome": "success",
        "complete": True,
        "reasons": [],
    }
    # Keys of the retired layout are assembled so source greps for them stay empty.
    return json.dumps(
        {"schema_version": 1, "last_success": attempt, "latest_completed" + "_attempt": attempt}
    )


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("broken", id="not-json"),
        pytest.param("[]", id="not-object"),
        pytest.param('{"schema_version":2}', id="missing-fields"),
        pytest.param('{"deep":true}', id="no-schema"),
        pytest.param('{"schema_version":true}', id="bool-schema"),
        pytest.param(
            _schema1_document(),
            id="schema-1",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema_version": 2,
                    "completed_at": "2000-01-01T00:00:00Z",
                    "booley_version": "1",
                    "image_id": None,
                    "outcome": "failed",
                    "reasons": 5,
                }
            ),
            id="reasons-not-a-list",
        ),
    ],
)
def test_invalid_or_legacy_state_is_due(tmp_path, monkeypatch, content):
    path = deep.deep_stamp_path(tmp_path)
    path.parent.mkdir()
    path.write_text(content)
    assert _codes(_observe_in(tmp_path, monkeypatch)) == ["no-qualifying-run"]


def test_atomic_replace_failure_preserves_old_state_and_cleans_temporary(tmp_path, monkeypatch):
    deep.record_deep_attempt(tmp_path, _record())
    original = deep.deep_stamp_path(tmp_path).read_bytes()
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("read-only")))
    assert not deep.record_deep_attempt(tmp_path, _record(deep.DeepOutcome.FAILED))
    assert deep.deep_stamp_path(tmp_path).read_bytes() == original
    assert not list(deep.deep_stamp_path(tmp_path).parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "name",
    [
        "booley.toml",
        "doctor-waivers.toml",
        "devcontainer.json",
        "design.core",
        "dut.sv",
        "tb.sv",
        "selftest/bad.sv",
    ],
)
def test_project_edits_and_plain_stamps_preserve_deep_currency(tmp_path, name):
    deep.record_deep_attempt(tmp_path, _record())
    before = deep.deep_stamp_path(tmp_path).read_bytes()
    edited = tmp_path / name
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text("edited input")
    doctor_stamp.record_clean_run(tmp_path, tmp_path, deep=False)
    assert deep.deep_stamp_path(tmp_path).read_bytes() == before
    assert deep.evaluate_deep_status(
        deep.load_deep_record(tmp_path), version=booley.__version__, image_id=IMAGE
    ).current


def _stub_orchestration(tmp_path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    project = SimpleNamespace(booley_dir=booley_dir, project_root=tmp_path)
    monkeypatch.setattr(doctor, "_run_project_phase", lambda *_args, **_kw: (None, project))
    monkeypatch.setattr(doctor, "_run_runtime_phase", lambda *_args: None)
    monkeypatch.setattr(doctor, "_run_flow_and_core_phase", lambda *_args: None)
    # Observe currency as an in-Sandbox Doctor does, with the matching image.
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: booley_dir)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    return booley_dir


@pytest.mark.parametrize(
    "missing,clean,outcome",
    [
        ((), True, deep.DeepOutcome.SUCCESS),
        (("developer-probe",), True, deep.DeepOutcome.INCOMPLETE),
        ((), False, deep.DeepOutcome.FAILED),
    ],
)
def test_doctor_records_only_completed_qualified_success(
    tmp_path, monkeypatch, missing, clean, outcome
):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)

    def run(*args):
        if not clean:
            args[-1].fail_("existing design verdict")
        return deep.DeepAttemptSummary(missing, (), IMAGE)

    monkeypatch.setattr(doctor, "_run_deep_phase", run)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert deep.load_deep_record(booley_dir).outcome == outcome
    assert result.deep_status.current is (outcome == deep.DeepOutcome.SUCCESS)
    assert result.counts["warn"] == 0
    assert result.exit_code == (0 if clean else 1)


def test_observation_only_deep_run_writes_neither_stamp(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    monkeypatch.setattr(
        doctor, "_run_deep_phase", lambda *_args: deep.DeepAttemptSummary((), (), IMAGE)
    )
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path, record_clean=False)
    assert not result.deep_status.current
    assert not deep.deep_stamp_path(booley_dir).exists()
    assert not doctor_stamp.stamp_path(booley_dir).exists()


def test_cancelled_run_leaves_matching_historical_success_current(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(booley_dir, _record())
    before = deep.deep_stamp_path(booley_dir).read_bytes()
    monkeypatch.setattr(doctor, "_run_deep_phase", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert deep.deep_stamp_path(booley_dir).read_bytes() == before
    assert doctor.observe_deep_status(tmp_path).current


@pytest.mark.parametrize("healthy", [False, True])
def test_plain_run_preserves_deep_state_and_never_runs_deep(tmp_path, monkeypatch, healthy):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(booley_dir, _record())
    before = deep.deep_stamp_path(booley_dir).read_bytes()
    monkeypatch.setattr(doctor, "_run_deep_phase", Mock(side_effect=AssertionError("deep")))
    if not healthy:
        monkeypatch.setattr(
            doctor, "_run_runtime_phase", lambda *args: args[-2].fail_("plain defect")
        )
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    assert result.deep_status.current
    assert deep.deep_stamp_path(booley_dir).read_bytes() == before
    assert result.clean is healthy


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("clean", [False, True])
def test_storage_failure_does_not_claim_new_current_evidence(tmp_path, monkeypatch, saved, clean):
    if saved:
        assert deep.record_deep_attempt(tmp_path, _record())
    path = deep.deep_stamp_path(tmp_path)
    before = path.read_bytes() if saved else None
    previous = deep.load_deep_record(tmp_path)
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    publication = Mock(side_effect=OSError("publication failed"))
    monkeypatch.setattr(Path, "replace", publication)
    messages = Mock()
    monkeypatch.setattr(doctor, "info", messages)
    findings = () if clean else (DiagnosticFinding(Severity.FAIL, "design failure"),)
    original = doctor.DoctorRunResult({}, findings, 0 if clean else 1)
    result = doctor._report_deep_currency(
        tmp_path,
        SimpleNamespace(booley_dir=tmp_path),
        original,
        deep.DeepAttemptSummary((), (), IMAGE),
        True,
    )
    assert result.deep_status.current is saved
    publication.assert_called_once()
    assert deep.load_deep_record(tmp_path) == previous
    assert (path.read_bytes() if path.exists() else None) == before
    assert result.clean is clean and result.exit_code == original.exit_code
    assert result.diagnostics == original.diagnostics
    assert [call.args[0] for call in messages.call_args_list] == [
        "deep result was not saved",
        result.deep_status.render(),
    ]
    document = result.deep_status.to_document()
    assert set(document) == {"status", "reasons"}
    assert document["status"] == ("current" if saved else "due")
    assert all(set(reason) == {"code", "message"} for reason in document["reasons"])


@pytest.mark.parametrize("writer", ["automatic", "manual"])
def test_persisted_reports_include_status_without_scheduling_or_dedup_changes(
    tmp_path, monkeypatch, writer
):
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text("[project]\nname='unit'\n")
    result = doctor._Reporter.create().result(0)
    result = replace(result, deep_status=_observe_in(booley_dir, monkeypatch))
    monkeypatch.setattr(doctor, "run_doctor_result", lambda *_args, **_kw: result)
    if writer == "automatic":
        report = auto_doctor._execute(tmp_path, booley_dir, "session-start")
    else:
        report = doctor.record_manual_result(tmp_path, result)
    assert report["deep_status"] == result.deep_status.to_document()
    assert set(report["deep_status"]) == {"status", "reasons"}
    # Deep status is metadata: changing it leaves the announcement identity alone.
    changed = {**report, "deep_status": {"status": "current"}}
    del changed["finding_hash"]
    health.report_path(booley_dir).write_text(json.dumps(changed))
    assert health.read_health(tmp_path).finding_hash == report["finding_hash"]
    assert health.compute_fingerprint(booley_dir, tmp_path) == report["fingerprint"]
    assert health.read_health(tmp_path).assessment.clean
    reset_cache()


def test_completed_invalid_config_attempt_vetoes_previous_success(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(booley_dir, _record())
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: booley_dir)

    def invalid(_root, reporter, **_kwargs):
        reporter.fail_("project configuration invalid")
        return None, None

    monkeypatch.setattr(doctor, "_run_project_phase", invalid)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert deep.load_deep_record(booley_dir).outcome == deep.DeepOutcome.FAILED
    assert not result.deep_status.current


@pytest.mark.parametrize(
    "host_version,sandbox_version,current,reason",
    [
        ("host-new", "validated", True, None),
        ("validated", "sandbox-new", False, "version-changed"),
        ("validated", None, False, "version-unavailable"),
    ],
)
def test_host_currency_uses_sandbox_execution_version(
    tmp_path, monkeypatch, host_version, sandbox_version, current, reason
):
    success = _record(version="validated")
    deep.record_deep_attempt(tmp_path, success)
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(booley, "__version__", host_version)
    probe = subprocess.CompletedProcess([], 0, f"{IMAGE}\n{sandbox_version or ''}\n", "")
    run = Mock(return_value=probe)
    monkeypatch.setattr(doctor.subprocess, "run", run)
    status = doctor.observe_deep_status(tmp_path, container="sandbox")
    assert status.current is current
    assert _codes(status) == ([] if reason is None else [reason])
    assert "sandbox" in run.call_args.args[0] and run.call_args[1]["stdin"] is subprocess.DEVNULL


SELECTED = SimpleNamespace(container_id="selected")


@pytest.mark.parametrize(
    "selected,stdout,identity",
    [
        (SELECTED, f"{IMAGE}\n1.0\n", (IMAGE, "1.0")),
        (SELECTED, "latest\n1.0\n", (None, "1.0")),
        (SELECTED, "", (None, None)),
        (None, "", (None, None)),
        (doctor.session_runtime.SessionError("multiple running Sandboxes"), "", (None, None)),
    ],
    ids=["selected", "malformed", "unreadable", "absent", "ambiguous"],
)
def test_host_identity_reads_selected_sandbox(tmp_path, monkeypatch, selected, stdout, identity):
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: False)
    resolve = Mock(side_effect=[selected])
    monkeypatch.setattr(doctor.session_runtime, "resolve_running_sandbox", resolve)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert doctor._observe_sandbox_identity(tmp_path, None) == identity
    assert all("selected" in call.args[0] for call in run.call_args_list)


def test_inside_currency_uses_live_package_version(tmp_path, monkeypatch):
    success = _record(version="validated")
    deep.record_deep_attempt(tmp_path, success)
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(booley, "__version__", "upgraded")
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    assert _codes(doctor.observe_deep_status(tmp_path)) == ["version-changed"]


def test_inside_identity_comes_from_issued_image_variable(tmp_path, monkeypatch):
    deep.record_deep_attempt(tmp_path, _record())
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    assert doctor.observe_deep_status(tmp_path).current
    monkeypatch.setenv(IMAGE_ENV, "latest")
    malformed = doctor.observe_deep_status(tmp_path)
    monkeypatch.delenv(IMAGE_ENV)
    for status in (malformed, doctor.observe_deep_status(tmp_path)):
        assert _codes(status) == ["image-identity-unavailable"]
        assert "cannot verify Sandbox Image identity" in status.render()


@pytest.mark.parametrize(
    "severity,identifier,current",
    [
        ("warn", "upgrade.review-pending", True),
        ("fail", "upgrade.review-pending", False),
        ("warn", "upgrade.runtime-stale", False),
    ],
)
def test_only_exact_pending_warning_is_exempt(
    tmp_path, monkeypatch, severity, identifier, current
):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    result = doctor.DoctorRunResult(
        {},
        (DiagnosticFinding(Severity(severity), "finding", check_id=identifier),),
        int(severity == "fail"),
    )
    reported = doctor._report_deep_currency(
        tmp_path,
        SimpleNamespace(booley_dir=booley_dir),
        result,
        deep.DeepAttemptSummary((), (), IMAGE),
        True,
    )
    assert reported.deep_status.current is current
    assert result.clean is False
    outcome = deep.load_deep_record(booley_dir).outcome
    assert outcome == (deep.DeepOutcome.SUCCESS if current else deep.DeepOutcome.FAILED)


@pytest.mark.parametrize("missing", [(), ("sim.selftest.good",)])
@pytest.mark.parametrize("waived", [False, True])
def test_waivers_never_supply_missing_execution(tmp_path, monkeypatch, missing, waived):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    finding = DiagnosticFinding(
        Severity.WAIVED if waived else Severity.WARN,
        "warning",
        check_id="flow.fail-path-unvalidated",
    )
    result = doctor.DoctorRunResult({}, (finding,), 0)
    reported = doctor._report_deep_currency(
        tmp_path,
        SimpleNamespace(booley_dir=booley_dir),
        result,
        deep.DeepAttemptSummary(missing, (), IMAGE),
        True,
    )
    assert reported.deep_status.current is (waived and not missing)
    expected = (
        []
        if not (missing or not waived)
        else (["latest-attempt-incomplete"] if waived else ["latest-attempt-failed"])
    )
    assert _codes(reported.deep_status) == expected


def test_typed_timestamp_uses_unknown_display_when_unparseable(tmp_path):
    assert deep.record_deep_attempt(tmp_path, replace(_record(), completed_at="invalid"))
    status = deep.evaluate_deep_status(
        deep.load_deep_record(tmp_path), version=booley.__version__, image_id=IMAGE
    )
    assert status.current
    assert status.render() == f"deep Doctor current (Booley {booley.__version__}, unknown)"
