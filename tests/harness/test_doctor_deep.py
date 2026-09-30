"""Deep currency depends only on qualified evidence, version, and active image."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.harness import auto_doctor, doctor, doctor_stamp
from booley.harness import doctor_deep as deep
from booley.runtime import sandbox_artifact

IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64


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
    assert _codes(status) == ["version-changed", "image-changed", "latest-attempt-failed"]
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
        ("completed_at", "invalid"),
        ("image_id", "tag:latest"),
        ("completion_order_ns", True),
        ("completion_order_ns", -1),
        ("complete", False),
        ("booley_version", "bad\ntext"),
        ("reasons", ["unbounded/arbitrary/text"]),
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project = SimpleNamespace(project_dir=project_dir, project_root=tmp_path)
    monkeypatch.setattr(doctor, "_run_project_phase", lambda *_args, **_kw: (None, project))
    monkeypatch.setattr(doctor, "_run_runtime_phase", lambda *_args: None)
    monkeypatch.setattr(doctor, "_run_flow_and_core_phase", lambda *_args: None)
    monkeypatch.setattr(
        sandbox_artifact,
        "observe",
        lambda *_args, **_kw: sandbox_artifact.SandboxArtifact(
            IMAGE, "container", booley.__version__, ""
        ),
    )
    monkeypatch.setattr(
        deep,
        "observe_deep_status",
        lambda *_args, **_kw: deep.evaluate_deep_status(
            deep.load_deep_state(project_dir), version=booley.__version__, image_id=IMAGE
        ),
    )
    return project_dir


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
    project_dir = _stub_orchestration(tmp_path, monkeypatch)

    def run(*args):
        if not clean:
            args[-1].fail_("existing design verdict")
        return deep.DeepAttemptSummary(missing, (), IMAGE)

    monkeypatch.setattr(doctor, "_run_deep_phase", run)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    state = deep.load_deep_state(project_dir)
    assert state.latest_completed_attempt.outcome == outcome
    assert result.deep_status.current is (outcome == deep.DeepOutcome.SUCCESS)
    assert result.counts["warn"] == 0
    assert result.exit_code == (0 if clean else 1)


def test_observation_only_deep_run_writes_neither_stamp(tmp_path, monkeypatch):
    project_dir = _stub_orchestration(tmp_path, monkeypatch)
    monkeypatch.setattr(
        doctor, "_run_deep_phase", lambda *_args: deep.DeepAttemptSummary((), (), IMAGE)
    )
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path, record_clean=False)
    assert not result.deep_status.current
    assert not deep.deep_stamp_path(project_dir).exists()
    assert not doctor_stamp.stamp_path(project_dir).exists()


def test_cancelled_run_leaves_matching_historical_success_current(tmp_path, monkeypatch):
    project_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(project_dir, _attempt())
    before = deep.deep_stamp_path(project_dir).read_bytes()
    monkeypatch.setattr(doctor, "_run_deep_phase", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert deep.deep_stamp_path(project_dir).read_bytes() == before
    assert deep.observe_deep_status(tmp_path).current


@pytest.mark.parametrize("healthy", [False, True])
def test_plain_run_preserves_deep_state_and_never_runs_deep(tmp_path, monkeypatch, healthy):
    project_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(project_dir, _attempt())
    before = deep.deep_stamp_path(project_dir).read_bytes()
    monkeypatch.setattr(doctor, "_run_deep_phase", Mock(side_effect=AssertionError("deep")))
    if not healthy:
        monkeypatch.setattr(
            doctor, "_run_runtime_phase", lambda *args: args[-2].fail_("plain defect")
        )
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    assert result.deep_status.current
    assert deep.deep_stamp_path(project_dir).read_bytes() == before
    assert result.clean is healthy


def test_storage_failure_does_not_claim_new_current_evidence(tmp_path, monkeypatch, capsys):
    _stub_orchestration(tmp_path, monkeypatch)
    monkeypatch.setattr(
        doctor, "_run_deep_phase", lambda *_args: deep.DeepAttemptSummary((), (), IMAGE)
    )
    monkeypatch.setattr(deep, "record_deep_attempt", lambda *_args: False)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert result.clean and result.exit_code == 0
    assert not result.deep_status.current and result.deep_status.storage_failed
    output = capsys.readouterr().out
    assert "deep evidence storage failed" in output
    assert "deep Doctor current" not in output


@pytest.mark.parametrize("writer", ["automatic", "manual"])
def test_persisted_reports_include_status_without_scheduling_or_dedup_changes(
    tmp_path, monkeypatch, writer
):
    from booley.runtime.project_dir import reset_cache

    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text("[project]\nname='unit'\n")
    result = doctor._Reporter.create().result(0)
    result = replace(
        result,
        deep_status=deep.evaluate_deep_status(
            deep.DeepState(), version=booley.__version__, image_id=IMAGE
        ),
    )
    monkeypatch.setattr(doctor, "run_doctor_result", lambda *_args, **_kw: result)
    if writer == "automatic":
        report = auto_doctor._execute(tmp_path, project_dir, "session-start")
    else:
        report = auto_doctor.record_manual_result(tmp_path, result)
    assert report["deep_status"] == result.deep_status.to_document()
    changed = {**report, "deep_status": {"status": "current"}}
    assert auto_doctor._finding_hash(report) == auto_doctor._finding_hash(changed)
    assert auto_doctor.compute_fingerprint(project_dir, tmp_path) == report["fingerprint"]
    assert auto_doctor.issue_counts(report) == (0, 0)
    reset_cache()


def test_completed_invalid_config_attempt_vetoes_previous_success(tmp_path, monkeypatch):
    project_dir = _stub_orchestration(tmp_path, monkeypatch)
    deep.record_deep_attempt(project_dir, _attempt())
    monkeypatch.setattr(doctor, "resolve_checkout_project_dir", lambda _: project_dir)

    def invalid(_root, reporter, **_kwargs):
        reporter.fail_("project configuration invalid")
        return None, None

    monkeypatch.setattr(doctor, "_run_project_phase", invalid)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    state = deep.load_deep_state(project_dir)
    assert state.last_success == _attempt()
    assert state.latest_completed_attempt.outcome == deep.DeepOutcome.FAILED
    assert not result.deep_status.current


def _write_in_process(arguments):
    project_dir, attempt = arguments
    return deep.record_deep_attempt(project_dir, attempt)


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


def test_unsaved_failure_is_due_in_this_invocation_without_erasing_history():
    success = _attempt()
    failure = _attempt(2, deep.DeepOutcome.FAILED)
    state = deep.DeepState(success, success)
    status = deep.evaluate_deep_status(
        state, version=booley.__version__, image_id=IMAGE, unsaved_attempt=failure
    )
    assert not status.current and status.storage_failed
    assert status.last_success == success
    assert _codes(status) == ["latest-attempt-failed"]
    assert state.latest_completed_attempt == success


def test_unsaved_success_cannot_establish_current_for_new_inputs():
    status = deep.evaluate_deep_status(
        deep.DeepState(), version=booley.__version__, image_id=IMAGE, unsaved_attempt=_attempt()
    )
    assert not status.current and status.storage_failed
    assert status.last_success is None
