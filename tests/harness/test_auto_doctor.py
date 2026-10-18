"""Tests for stale-triggered automatic Doctor execution and reporting."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.harness import auto_doctor, doctor
from booley.runtime import doctor_stamp, health, health_presentation
from booley.runtime.booley_dir import reset_cache, resolve_booley_dir
from booley.runtime.timefmt import MACHINE_TIMESTAMP_FORMAT


@pytest.fixture(autouse=True)
def _isolated_project_resolution(monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _project(root: Path) -> Path:
    booley_dir = root / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text("[project]\nname = 'unit'\n", encoding="utf-8")
    return booley_dir


def _mounted_view(root: Path, view: Path) -> Path:
    try:
        view.symlink_to(root, target_is_directory=True)
    except OSError:
        shutil.copytree(root, view)
    return view


def _report(root: Path, *, clean: bool, checked_at: datetime) -> dict:
    booley_dir = root / ".booley"
    findings = []
    counts = {"pass": 1, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0}
    if not clean:
        counts.update({"pass": 0, "fail": 1})
        findings = [
            {
                "severity": "fail",
                "message": "sandbox image missing",
                "fix": "run booley init",
                "check_id": None,
                "subject": None,
            }
        ]
    payload = {
        "checked_at": checked_at.strftime(MACHINE_TIMESTAMP_FORMAT),
        "clean": clean,
        "counts": counts,
        "findings": findings,
        "fingerprint": health.compute_fingerprint(booley_dir, root),
    }
    path = health.report_path(booley_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    # A report without a stored hash is identified by the hash of its active findings.
    payload["finding_hash"] = health.read_health(root).finding_hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class TestDueReason:
    def test_config_change_names_changed_input(self, tmp_path: Path):
        booley_dir = _project(tmp_path)
        _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
        (booley_dir / "booley.toml").write_text("[project]\nname = 'edited'\n")

        assert "booley.toml" in health.due_reason(tmp_path), "input drift must name booley.toml"

    def test_missing_result_is_due_without_clean_manual_stamp(self, tmp_path: Path):
        _project(tmp_path)

        assert health.due_reason(tmp_path) == "no automatic Doctor result"

    def test_manual_stamp_does_not_replace_runtime_scoped_result(self, tmp_path: Path):
        booley_dir = _project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)

        assert health.due_reason(tmp_path) == "no automatic Doctor result"

    def test_unhealthy_result_retries_daily(self, tmp_path: Path):
        _project(tmp_path)
        now = datetime.now(tz=UTC)
        _report(tmp_path, clean=False, checked_at=now - timedelta(hours=23))

        assert health.due_reason(tmp_path, now=now) is None

        _report(tmp_path, clean=False, checked_at=now - timedelta(hours=25))
        assert health.due_reason(tmp_path, now=now) == "automatic Doctor result expired"

    def test_upgrade_only_warning_uses_weekly_cadence(self, tmp_path: Path):
        _project(tmp_path)
        now = datetime.now(tz=UTC)
        payload = _report(tmp_path, clean=False, checked_at=now - timedelta(days=2))
        payload["counts"].update({"fail": 0, "warn": 1})
        payload["findings"] = [
            {
                "severity": "warn",
                "message": "release review pending",
                "fix": "invoke heal",
                "check_id": "upgrade.review-pending",
                "subject": None,
            }
        ]
        del payload["finding_hash"]  # the fixture hashed its earlier FAIL finding
        health.report_path(tmp_path / ".booley").write_text(json.dumps(payload), encoding="utf-8")

        assert health.due_reason(tmp_path, now=now) is None

    def test_config_change_invalidates_result(self, tmp_path: Path):
        booley_dir = _project(tmp_path)
        _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
        (booley_dir / "booley.toml").write_text("[project]\nname = 'edited'\n", encoding="utf-8")

        assert health.due_reason(tmp_path) == "Doctor inputs changed: booley.toml"

    def test_upgrade_review_state_change_invalidates_result(self, tmp_path: Path):
        booley_dir = _project(tmp_path)
        _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
        state = booley_dir / "runtime" / "upgrade_review.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"schema": 1, "reviewed_through": "1.0.0"}), encoding="utf-8")

        assert health.due_reason(tmp_path) == "Doctor inputs changed: upgrade review"


def test_fingerprint_ignores_project_mount_basename(tmp_path: Path):
    host = tmp_path / "host-project"
    host.mkdir()
    directory = _project(host)
    (host / "design.core").write_text("project core")
    (directory / "cores").mkdir()
    (directory / "cores" / "design.core").write_text("Booley core")
    sandbox = tmp_path / "work"
    _mounted_view(host, sandbox)
    host_fingerprint = health.compute_fingerprint(resolve_booley_dir(host), host)
    reset_cache()
    sandbox_fingerprint = health.compute_fingerprint(resolve_booley_dir(sandbox), sandbox)

    assert host_fingerprint == sandbox_fingerprint, (
        "identical mounted inputs must share a fingerprint"
    )


def test_core_digest_distinguishes_entry_boundaries(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    directory = _project(root)
    first = root / "a.core"
    second = root / "b.core"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    before = health.compute_fingerprint(directory, root)
    first.unlink()
    second.unlink()
    joined = root / ("a.core" + hashlib.sha256(b"A").hexdigest() + "project") / "b.core"
    joined.parent.mkdir(parents=True)
    joined.write_bytes(b"B")

    assert health.compute_fingerprint(directory, root) != before, (
        "different core collections must have different fingerprints"
    )


@pytest.mark.parametrize("mutation", ["edit", "add", "delete", "rename", "origin"])
def test_core_digest_tracks_authored_changes(tmp_path, mutation):
    directory = _project(tmp_path)
    (directory / "cores").mkdir()
    core = tmp_path / "design.core"
    core.write_text("first")
    before = health.compute_fingerprint(directory, tmp_path)
    if mutation == "edit":
        core.write_text("second")
    elif mutation == "add":
        (tmp_path / "added.core").write_text("first")
    elif mutation == "delete":
        core.unlink()
    elif mutation == "rename":
        core.rename(tmp_path / "renamed.core")
    else:
        core.rename(directory / "cores" / "design.core")
    assert health.compute_fingerprint(directory, tmp_path) != before


def test_core_origins_with_equal_relative_paths_remain_distinct(tmp_path):
    directory = _project(tmp_path)
    (directory / "cores").mkdir()
    project_core = tmp_path / "design.core"
    booley_core = directory / "cores" / "design.core"
    project_core.write_text("project")
    booley_core.write_text("booley")
    before = health.compute_fingerprint(directory, tmp_path)
    project_core.write_text("booley")
    booley_core.write_text("project")
    assert health.compute_fingerprint(directory, tmp_path) != before


def test_fingerprint_labels_cover_every_input(tmp_path):
    directory = _project(tmp_path)
    assert set(health.compute_fingerprint(directory, tmp_path)) == set(health._INPUT_LABELS)


def test_changed_input_names_are_ordered_and_bounded(tmp_path):
    directory = _project(tmp_path)
    _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
    for name in ("tests.toml", "configs.toml", "booley.toml", "doctor-waivers.toml"):
        (directory / name).write_text("changed")
    assert health.due_reason(tmp_path) == (
        "Doctor inputs changed: booley.toml, doctor-waivers.toml, tests.toml, +1 more"
    )


@pytest.mark.parametrize("change", ["missing", "extra", "malformed", "absent"])
def test_incompatible_fingerprints_use_safe_generic_reason(tmp_path, change):
    directory = _project(tmp_path)
    document = _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
    recorded = document["fingerprint"]
    if change == "missing":
        del recorded["booley_toml_sha256"]
    elif change == "extra":
        recorded["/secret/hostile-name"] = "secret content"
    elif change == "malformed":
        document["fingerprint"] = ["/secret/hostile-name"]
    else:
        del document["fingerprint"]
    health.report_path(directory).write_text(json.dumps(document))
    assert health.due_reason(tmp_path) == "Doctor inputs changed"


def test_execute_persists_structured_report_and_transcript(tmp_path: Path, monkeypatch):
    booley_dir = _project(tmp_path)
    result = doctor.DoctorRunResult(
        counts={"pass": 2, "fail": 0, "warn": 1, "waived": 0, "note": 0, "skip": 0},
        findings=(
            DiagnosticFinding(
                Severity.WARN, "license may expire", "renew it", "agent.credential", "claude"
            ),
        ),
        exit_code=0,
    )

    def run_doctor(*_args, progress=None, **_kwargs):
        print("captured Doctor finding")
        progress("host/project checks")
        return result

    monkeypatch.setattr(doctor, "run_doctor_result", run_doctor)
    progress = []

    payload = auto_doctor._execute(
        tmp_path,
        booley_dir,
        "test",
        progress=progress.append,
    )

    assert payload["clean"] is False
    assert payload["trigger"] == "test"
    assert payload["findings"][0]["check_id"] == "agent.credential"
    assert health.load_report(tmp_path) == payload
    assert health.transcript_path(booley_dir).is_file()
    assert "captured Doctor finding" in health.transcript_path(booley_dir).read_text()
    assert progress[0] == "host/project checks"
    assert progress[-1].startswith("completed in ")
    assert "transcript:" in progress[-1]


def test_manual_runtime_result_replaces_stale_automatic_status(tmp_path: Path):
    _project(tmp_path)
    _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))
    result = doctor.DoctorRunResult(
        counts={"pass": 3, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0},
        findings=(DiagnosticFinding(Severity.PASS, "environment healthy"),),
        exit_code=0,
    )

    payload = doctor.record_manual_result(tmp_path, result)

    assert payload is not None and payload["clean"] is True
    assert payload["trigger"] == "manual-doctor"
    assert "clean" in health_presentation.current_summary(tmp_path)


def test_manual_smoke_result_does_not_replace_health_status(tmp_path: Path):
    _project(tmp_path)
    previous = _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))
    result = doctor.DoctorRunResult(
        counts={"pass": 3, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 3},
        findings=(DiagnosticFinding(Severity.SKIP, "agent credential checks skipped"),),
        exit_code=0,
        health_evidence=False,
    )

    assert doctor.record_manual_result(tmp_path, result) is None
    assert health.load_report(tmp_path) == previous


def test_changed_summary_is_consumed_once_per_channel(tmp_path: Path):
    _project(tmp_path)
    report = _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))
    announcements = health.state_dir(tmp_path / ".booley") / "announcements.json"
    announcements.write_text(json.dumps({"ntfy": report["finding_hash"]}))

    first = health_presentation.consume_changed_summary(tmp_path, channel="mcp", issues_only=True)
    second = health_presentation.consume_changed_summary(tmp_path, channel="mcp", issues_only=True)
    other = health_presentation.consume_changed_summary(
        tmp_path, channel="runner", issues_only=True
    )

    assert first is not None and "1 FAIL" in first
    assert second is None
    assert other == first
    assert json.loads(announcements.read_text())["ntfy"] == report["finding_hash"]


def test_launch_skips_when_current(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
    spawned = []
    monkeypatch.setattr(auto_doctor.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert auto_doctor.launch(tmp_path) == "current"
    assert spawned == []


def test_launch_starts_detached_worker_when_due(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    spawned = []
    monkeypatch.setattr(auto_doctor.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert auto_doctor.launch(tmp_path) == "started"
    assert spawned[0][1]["start_new_session"] is True
    assert "booley.harness.auto_doctor" in spawned[0][0][0]


def test_run_if_due_records_once_then_stays_current(tmp_path: Path, monkeypatch):
    booley_dir = _project(tmp_path)
    (booley_dir / "booley.toml").write_text(
        '[project]\nname = "unit"\n[notifications]\nntfy_topic = "probe"\n'
    )
    calls = []
    deliveries = []
    monkeypatch.setattr(auto_doctor.subprocess, "Popen", lambda *a, **k: deliveries.append(a))

    def execute(root, _booley_dir, trigger, **_kwargs):
        calls.append(trigger)
        return _report(root, clean=True, checked_at=datetime.now(tz=UTC))

    monkeypatch.setattr(auto_doctor, "_execute", execute)

    progress = []
    assert (
        auto_doctor.run_if_due(
            tmp_path,
            trigger="test",
            progress=progress.append,
        )
        is not None
    )
    assert auto_doctor.run_if_due(tmp_path, trigger="test") is None
    assert calls == ["test"]
    assert deliveries == []
    assert health.load_report(tmp_path) is not None
    assert progress == ["starting (no automatic Doctor result)"]


def test_containment_alias_core_exclusion(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    selected = tmp_path / "selected"
    alias = root / "data-alias"
    for directory in (selected, alias):
        directory.mkdir(parents=True)
    core = alias / "generated.core"
    core.write_text("first")
    original = Path.samefile
    calls = []

    def samefile(path, other):
        if {path, Path(other)} == {selected, alias}:
            calls.append(path)
            return True
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    before = health._core_digest(root, selected)
    core.write_text("second")
    assert health._core_digest(root, selected) == before
    assert calls


def test_containment_checkout_core_symlink_remains_fingerprinted(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    selected = tmp_path / "selected"
    selected.mkdir()
    core = selected / "generated.core"
    core.write_text("first")
    (root / "duplicate.core").symlink_to(core)
    before = health._core_digest(root, selected)
    core.write_text("second")
    assert health._core_digest(root, selected) != before
