"""Deep currency depends only on qualified evidence, version, and active image."""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.harness import auto_doctor, doctor
from booley.harness import doctor_deep as deep
from booley.runtime import doctor_stamp, health

IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64
IMAGE_ENV = "BOOLEY_SANDBOX_IMAGE_ID"


def _attempt(order=1, outcome=deep.DeepOutcome.SUCCESS, *, version=None, image=IMAGE):
    return deep.DeepAttempt(
        "2000-01-01T00:00:00Z",
        order,
        str(order),
        version or booley.__version__,
        image,
        outcome,
        outcome == deep.DeepOutcome.SUCCESS,
        () if outcome == deep.DeepOutcome.SUCCESS else ("sim.selftest.bad",),
    )


def _codes(status):
    return [reason.code for reason in status.reasons]


def test_matching_old_success_never_expires():
    attempt = _attempt()
    status = deep.evaluate_deep_status(
        deep.DeepState(attempt, attempt), version=booley.__version__, image_id=IMAGE
    )
    assert status.current
    assert "01 JAN 2000" in status.render()


def test_all_applicable_reasons_are_reported_and_success_retained():
    success, failed = _attempt(version="old"), _attempt(2, deep.DeepOutcome.FAILED)
    status = deep.evaluate_deep_status(
        deep.DeepState(success, failed), version="new", image_id=OTHER_IMAGE
    )
    assert _codes(status) == [
        "version-changed",
        "image-changed",
        "latest-attempt-not-qualified",
        "latest-attempt-incomplete",
    ]
    assert status.last_success == success


@pytest.mark.parametrize("outcome", [deep.DeepOutcome.FAILED, deep.DeepOutcome.INCOMPLETE])
def test_unsuccessful_attempt_veto_survives_switching_inputs_back(tmp_path, outcome):
    assert deep.record_deep_attempt(tmp_path, _attempt())
    assert deep.record_deep_attempt(tmp_path, _attempt(3, outcome, image=OTHER_IMAGE))
    status = deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    )
    assert not status.current
    assert status.last_success == _attempt()
    assert deep.record_deep_attempt(tmp_path, _attempt(4))
    assert deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    ).current


def test_no_run_and_unverifiable_image_are_both_reported():
    status = deep.evaluate_deep_status(deep.DeepState(), version="new", image_id=None)
    assert _codes(status) == ["no-qualifying-run", "image-identity-unavailable"]


@pytest.mark.parametrize(
    "content", ["broken", "[]", '{"schema_version":2}', '{"deep":true}', '{"schema_version":true}']
)
def test_invalid_or_legacy_state_is_due(tmp_path, content):
    path = deep.deep_stamp_path(tmp_path)
    path.parent.mkdir()
    path.write_text(content)
    assert deep.load_deep_state(tmp_path) == deep.DeepState()


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed_at", 1),
        ("image_id", False),
        ("completion_order_ns", True),
        ("complete", "true"),
        ("booley_version", []),
        ("reasons", [1]),
        ("failure_count", None),
    ],
)
def test_corrupt_success_never_establishes_currency(tmp_path, field, value):
    deep.record_deep_attempt(tmp_path, _attempt())
    path = deep.deep_stamp_path(tmp_path)
    document = json.loads(path.read_text())
    document["last_success"][field] = value
    path.write_text(json.dumps(document))
    assert deep.load_deep_state(tmp_path) == deep.DeepState()


def test_older_success_arriving_after_newer_failure_is_retained(tmp_path):
    deep.record_deep_attempt(tmp_path, _attempt(3, deep.DeepOutcome.FAILED))
    deep.record_deep_attempt(tmp_path, _attempt(2))
    state = deep.load_deep_state(tmp_path)
    assert state.last_success == _attempt(2)
    assert state.latest_completed_attempt == _attempt(3, deep.DeepOutcome.FAILED)


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_completion_tie_favors_unsuccessful_evidence(tmp_path, reverse):
    attempts = [_attempt(2), replace(_attempt(2, deep.DeepOutcome.INCOMPLETE), attempt_id="0")]
    for attempt in reversed(attempts) if reverse else attempts:
        deep.record_deep_attempt(tmp_path, attempt)
    state = deep.load_deep_state(tmp_path)
    assert state.last_success == attempts[0]
    assert state.latest_completed_attempt == attempts[1]


def test_concurrent_writers_merge_success_and_veto_independently(tmp_path):
    attempts = [
        _attempt(i, deep.DeepOutcome.SUCCESS if i % 2 else deep.DeepOutcome.FAILED)
        for i in range(1, 21)
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(
            executor.map(lambda attempt: deep.record_deep_attempt(tmp_path, attempt), attempts)
        )
    state = deep.load_deep_state(tmp_path)
    assert state.last_success == attempts[-2]
    assert state.latest_completed_attempt == attempts[-1]
    assert not list(deep.deep_stamp_path(tmp_path).parent.glob("*.tmp"))


def test_atomic_replace_failure_preserves_old_state_and_cleans_temporary(tmp_path, monkeypatch):
    deep.record_deep_attempt(tmp_path, _attempt())
    original = deep.deep_stamp_path(tmp_path).read_bytes()
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("read-only")))
    assert not deep.record_deep_attempt(tmp_path, _attempt(2, deep.DeepOutcome.FAILED))
    assert deep.deep_stamp_path(tmp_path).read_bytes() == original
    assert not list(deep.deep_stamp_path(tmp_path).parent.glob(".*.tmp"))


def test_lock_timeout_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(deep, "wait_for_file_lock", Mock(side_effect=TimeoutError))
    assert not deep.record_deep_attempt(tmp_path, _attempt())
    assert deep.load_deep_state(tmp_path) == deep.DeepState()


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
    deep.record_deep_attempt(tmp_path, _attempt())
    before = deep.deep_stamp_path(tmp_path).read_bytes()
    edited = tmp_path / name
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text("edited input")
    doctor_stamp.record_clean_run(tmp_path, tmp_path, deep=False)
    assert deep.deep_stamp_path(tmp_path).read_bytes() == before
    assert deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    ).current


def _stub_orchestration(tmp_path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    project = SimpleNamespace(booley_dir=booley_dir, project_root=tmp_path)
    monkeypatch.setattr(doctor, "_run_project_phase", lambda *_args, **_kw: (None, project))
    monkeypatch.setattr(doctor, "_run_runtime_phase", lambda *_args: None)
    monkeypatch.setattr(doctor, "_run_flow_and_core_phase", lambda *_args: None)
    monkeypatch.setattr(
        doctor,
        "observe_deep_status",
        lambda *_args, **_kw: deep.evaluate_deep_status(
            deep.load_deep_state(booley_dir), version=booley.__version__, image_id=IMAGE
        ),
    )
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
    state = deep.load_deep_state(booley_dir)
    assert state.latest_completed_attempt.outcome == outcome
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
    deep.record_deep_attempt(booley_dir, _attempt())
    before = deep.deep_stamp_path(booley_dir).read_bytes()
    monkeypatch.setattr(doctor, "_run_deep_phase", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert deep.deep_stamp_path(booley_dir).read_bytes() == before
    assert doctor.observe_deep_status(tmp_path).current


@pytest.mark.parametrize("healthy", [False, True])
def test_plain_run_preserves_deep_state_and_never_runs_deep(tmp_path, monkeypatch, healthy):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(booley_dir, _attempt())
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
    from booley.audit.diagnostic_results import DiagnosticFinding, Severity

    if saved:
        assert deep.record_deep_attempt(tmp_path, _attempt())
    path = deep.deep_stamp_path(tmp_path)
    before = path.read_bytes() if saved else None
    previous = deep.load_deep_state(tmp_path)
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
    assert deep.load_deep_state(tmp_path) == previous
    assert (path.read_bytes() if path.exists() else None) == before
    assert result.clean is clean and result.exit_code == original.exit_code
    assert result.diagnostics == original.diagnostics
    assert [call.args[0] for call in messages.call_args_list] == [
        "deep result was not saved",
        result.deep_status.render(),
    ]
    document = result.deep_status.to_document()
    assert set(document) == {"status", "last_success", "reasons"}
    assert document["status"] == ("current" if saved else "due")
    assert all(set(reason) == {"code", "message", "checks"} for reason in document["reasons"])


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
    result = replace(
        result,
        deep_status=deep.evaluate_deep_status(
            deep.DeepState(), version=booley.__version__, image_id=IMAGE
        ),
    )
    monkeypatch.setattr(doctor, "run_doctor_result", lambda *_args, **_kw: result)
    if writer == "automatic":
        report = auto_doctor._execute(tmp_path, booley_dir, "session-start")
    else:
        report = doctor.record_manual_result(tmp_path, result)
    assert report["deep_status"] == result.deep_status.to_document()
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
    deep.record_deep_attempt(booley_dir, _attempt())
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: booley_dir)

    def invalid(_root, reporter, **_kwargs):
        reporter.fail_("project configuration invalid")
        return None, None

    monkeypatch.setattr(doctor, "_run_project_phase", invalid)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    state = deep.load_deep_state(booley_dir)
    assert state.last_success == _attempt()
    assert state.latest_completed_attempt.outcome == deep.DeepOutcome.FAILED
    assert not result.deep_status.current


def _write_in_process(arguments):
    booley_dir, attempt = arguments
    return deep.record_deep_attempt(booley_dir, attempt)


def test_separate_processes_share_the_project_evidence_lock(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    attempts = [_attempt(2, deep.DeepOutcome.FAILED), _attempt(1), _attempt(3)]
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        assert all(executor.map(_write_in_process, [(tmp_path, attempt) for attempt in attempts]))
    state = deep.load_deep_state(tmp_path)
    assert state.last_success == attempts[-1]
    assert state.latest_completed_attempt == attempts[-1]


def test_interruption_during_publication_preserves_record_and_releases_lock(tmp_path, monkeypatch):
    deep.record_deep_attempt(tmp_path, _attempt())
    before = deep.deep_stamp_path(tmp_path).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", Mock(side_effect=KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            deep.record_deep_attempt(tmp_path, _attempt(2, deep.DeepOutcome.FAILED))
    assert deep.deep_stamp_path(tmp_path).read_bytes() == before
    assert deep.record_deep_attempt(tmp_path, _attempt(3))


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
    success = _attempt(version="validated")
    deep.record_deep_attempt(tmp_path, success)
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(booley, "__version__", host_version)
    probe = subprocess.CompletedProcess([], 0, f"{IMAGE}\n{sandbox_version or ''}\n", "")
    observation = Mock(return_value=probe)
    monkeypatch.setattr(doctor.subprocess, "run", observation)
    status = doctor.observe_deep_status(tmp_path, container="sandbox")
    assert status.current is current
    assert _codes(status) == ([] if reason is None else [reason])
    assert "sandbox" in observation.call_args.args[0]


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
    success = _attempt(version="validated")
    deep.record_deep_attempt(tmp_path, success)
    monkeypatch.setattr(doctor, "resolve_checkout_booley_dir", lambda _: tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(booley, "__version__", "upgraded")
    monkeypatch.setenv(IMAGE_ENV, IMAGE)
    assert _codes(doctor.observe_deep_status(tmp_path)) == ["version-changed"]


def test_inside_identity_comes_from_issued_image_variable(tmp_path, monkeypatch):
    deep.record_deep_attempt(tmp_path, _attempt())
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


def test_warning_only_attempt_names_warning_without_claiming_failure(tmp_path, monkeypatch):
    booley_dir = _stub_orchestration(tmp_path, monkeypatch)

    def run(*args):
        args[-1].warn_(doctor.warning("git.worktree-portability", "policy warning"))
        return deep.DeepAttemptSummary((), (), IMAGE)

    monkeypatch.setattr(doctor, "_run_deep_phase", run)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert result.exit_code == 0 and not result.clean
    assert "latest-attempt-not-qualified" in _codes(result.deep_status)
    assert "git.worktree-portability" in result.deep_status.render()
    assert "failed" not in result.deep_status.render()
    assert (
        deep.load_deep_state(booley_dir).latest_completed_attempt.outcome
        == deep.DeepOutcome.FAILED
    )


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
    from booley.audit.diagnostic_results import DiagnosticFinding, Severity

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
    attempt = deep.load_deep_state(booley_dir).latest_completed_attempt
    assert attempt.failure_count == int(severity == "fail")


@pytest.mark.parametrize("missing", [(), ("sim.selftest.good",)])
@pytest.mark.parametrize("waived", [False, True])
def test_waivers_never_supply_missing_execution(tmp_path, monkeypatch, missing, waived):
    from booley.audit.diagnostic_results import DiagnosticFinding, Severity

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
    codes = _codes(reported.deep_status)
    assert ("latest-attempt-incomplete" in codes) is bool(missing)
    assert ("latest-attempt-not-qualified" in codes) is (not waived)


@pytest.mark.parametrize("reasons", [(), ("sim.selftest.bad",)])
def test_legacy_failed_health_is_unknown_and_never_migrated(tmp_path, reasons):
    attempt = replace(_attempt(2, deep.DeepOutcome.FAILED), reasons=reasons)
    assert deep.record_deep_attempt(tmp_path, _attempt())
    assert deep.record_deep_attempt(tmp_path, attempt)
    before = deep.deep_stamp_path(tmp_path).read_bytes()
    status = deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    )
    assert "latest-attempt-not-qualified" in _codes(status)
    assert "legacy health verdict details unavailable" in status.render()
    assert "failed" not in status.render()
    assert deep.deep_stamp_path(tmp_path).read_bytes() == before


def test_mixed_actual_failure_warning_and_missing_execution_are_preserved(tmp_path):
    attempt = deep.DeepAttemptSummary(("sim.selftest.bad",), (), IMAGE).attempt(
        version=booley.__version__,
        clean=False,
        failure_count=2,
        blocking_warning_ids=("git.worktree-portability",),
    )
    assert deep.record_deep_attempt(tmp_path, attempt)
    status = deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    )
    assert "latest-attempt-failed" in _codes(status)
    assert "latest-attempt-incomplete" in _codes(status)
    assert "2 failure(s)" in status.render()
    assert "git.worktree-portability" in status.render()
    assert "sim.selftest.bad" in status.render()


def _legacy_decode_attempt(raw: object) -> deep.DeepAttempt | None:
    # Frozen schema-1 decoder: optional health fields did not exist here.
    import re

    from booley.core.boundary import (
        BoundaryError,
        require_bool,
        require_dict,
        require_int,
        require_list,
        require_opt_str,
        require_str,
        require_str_value,
    )
    from booley.runtime.image_provenance import is_local_image_id
    from booley.runtime.timefmt import parse_timestamp

    _SAFE_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,192}")
    _VERSION = re.compile(r"[a-zA-Z0-9_.+-]{1,128}")
    _MAX_REASONS = 64
    if raw is None:
        return None
    values = require_dict(raw, field="deep attempt")
    completed_at = require_str(values, "completed_at")
    parse_timestamp(completed_at)
    order = require_int(values.get("completion_order_ns"), field="completion_order_ns")
    attempt_id, version = require_str(values, "attempt_id"), require_str(values, "booley_version")
    image_id = require_opt_str(values, "image_id")
    complete = require_bool(values, "complete")
    outcome = deep.DeepOutcome(require_str(values, "outcome"))
    reasons = tuple(require_str_value(item) for item in require_list(values.get("reasons")))
    if order <= 0 or not _SAFE_ID.fullmatch(attempt_id) or not _VERSION.fullmatch(version):
        raise BoundaryError("invalid deep ordering or version")
    if image_id is not None and not is_local_image_id(image_id):
        raise BoundaryError("invalid deep image identity")
    if len(reasons) > _MAX_REASONS or any(not _SAFE_ID.fullmatch(item) for item in reasons):
        raise BoundaryError("invalid deep reason identifiers")
    if outcome == deep.DeepOutcome.SUCCESS and (not complete or image_id is None or reasons):
        raise BoundaryError("success lacks qualifying execution evidence")
    return deep.DeepAttempt(
        completed_at, order, attempt_id, version, image_id, outcome, complete, reasons
    )


def _legacy_merge(state: deep.DeepState, attempt: deep.DeepAttempt) -> deep.DeepState:
    success, latest = state.last_success, state.latest_completed_attempt
    if attempt.outcome == deep.DeepOutcome.SUCCESS and (
        success is None or attempt.order > success.order
    ):
        success = attempt
    if latest is None or attempt.order > latest.order:
        latest = attempt
    return deep.DeepState(success, latest)


def test_frozen_legacy_reader_and_merger_retain_success_with_new_warning_record(tmp_path):
    success = deep.DeepAttemptSummary((), (), IMAGE).attempt(
        version=booley.__version__, clean=True, failure_count=0, blocking_warning_ids=()
    )
    warning = deep.DeepAttemptSummary((), (), IMAGE).attempt(
        version=booley.__version__,
        clean=False,
        failure_count=0,
        blocking_warning_ids=("git.worktree-portability",),
    )
    assert deep.record_deep_attempt(tmp_path, success)
    assert deep.record_deep_attempt(tmp_path, warning)
    document = json.loads(deep.deep_stamp_path(tmp_path).read_text())
    legacy_success = _legacy_decode_attempt(document["last_success"])
    legacy_warning = _legacy_decode_attempt(document["latest_completed_attempt"])
    assert legacy_warning.outcome is deep.DeepOutcome.FAILED
    merged = _legacy_merge(deep.DeepState(legacy_success, legacy_success), legacy_warning)
    assert merged.last_success == legacy_success
    assert merged.latest_completed_attempt == legacy_warning
    assert not deep.evaluate_deep_status(
        merged, version=booley.__version__, image_id=IMAGE
    ).current


@pytest.mark.parametrize("slot", [None, "last_success", "latest_completed_attempt", "complete"])
def test_missing_record_fields_are_due(tmp_path, slot):
    assert deep.load_deep_state(tmp_path) == deep.DeepState()
    assert deep.record_deep_attempt(tmp_path, _attempt())
    path = deep.deep_stamp_path(tmp_path)
    document = json.loads(path.read_text())
    if slot is None or slot == "complete":
        del document["last_success"][slot or "image_id"]
    else:
        del document[slot]
    path.write_text(json.dumps(document))
    assert deep.load_deep_state(tmp_path) == deep.DeepState()


@pytest.mark.parametrize(
    "updates",
    [
        {"attempt_id": "unrestricted/id"},
        {"booley_version": "unrestricted/version"},
        {"image_id": "tag:latest"},
        {"completion_order_ns": -1},
        {"complete": False},
        {"outcome": "incomplete"},
        {"reasons": ["unrestricted/reason"] * 65},
        {"failure_count": -1, "blocking_warning_count": 0, "blocking_warning_ids": []},
        {"failure_count": 0, "blocking_warning_count": 2**40, "blocking_warning_ids": ["x"]},
        {"failure_count": 0, "blocking_warning_count": 0, "blocking_warning_ids": ["x", "x"]},
    ],
)
def test_typed_published_record_is_trusted(tmp_path, updates):
    assert deep.record_deep_attempt(tmp_path, _attempt())
    path = deep.deep_stamp_path(tmp_path)
    document = json.loads(path.read_text())
    document["last_success"].update(updates)
    path.write_text(json.dumps(document))
    state = deep.load_deep_state(tmp_path)
    assert state.last_success is not None
    for field, value in updates.items():
        assert getattr(state.last_success, field) == (
            tuple(value) if isinstance(value, list) else value
        )


def test_warning_display_keeps_first_eight_sorted_ids(tmp_path):
    ids = ("unrestricted/id", *tuple(f"warning-{i}" for i in range(10)), "warning-0")
    attempt = deep.DeepAttemptSummary((), (), IMAGE).attempt(
        version=booley.__version__, clean=False, failure_count=0, blocking_warning_ids=ids
    )
    assert attempt.blocking_warning_ids == tuple(sorted(set(ids)))[:8]
    assert attempt.blocking_warning_count == len(ids)
    assert deep.record_deep_attempt(tmp_path, attempt)
    assert deep.load_deep_state(tmp_path).latest_completed_attempt == attempt


def test_typed_timestamp_uses_unknown_display_when_unparseable(tmp_path):
    assert deep.record_deep_attempt(tmp_path, _attempt())
    path = deep.deep_stamp_path(tmp_path)
    document = json.loads(path.read_text())
    document["last_success"]["completed_at"] = "invalid"
    path.write_text(json.dumps(document))
    status = deep.evaluate_deep_status(
        deep.load_deep_state(tmp_path), version=booley.__version__, image_id=IMAGE
    )
    assert status.current
    assert status.render() == f"deep Doctor current (Booley {booley.__version__}, unknown)"
