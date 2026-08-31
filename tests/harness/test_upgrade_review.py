"""Durable upgrade-review state and compare-and-swap contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from booley.harness import doctor, upgrade_review

_NOW = "2026-08-31T12:00:00Z"


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / ".booley_project"
    (project_dir / "runtime").mkdir(parents=True)
    return project_dir


def _changelog(tmp_path: Path, version: str = "2.0.0") -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        f"# Changelog\n\n## {version} - 31 AUG 2026\n\n### Upgrade notes\n\n- Review.\n",
        encoding="utf-8",
    )
    return path


def _payload(project_dir: Path) -> dict:
    return json.loads(upgrade_review.state_path(project_dir).read_text(encoding="utf-8"))


def test_fresh_install_establishes_quiet_baseline(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)

    status = upgrade_review.observe(project_dir, current_version="1.2.3", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.CURRENT
    assert _payload(project_dir) == {"schema": 1, "reviewed_through": "1.2.3"}


def test_old_doctor_stamp_bootstraps_pending_upgrade(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    (project_dir / "runtime" / "doctor_stamp.json").write_text(
        json.dumps({"booley_version": "1.2.3"}), encoding="utf-8"
    )

    status = upgrade_review.observe(project_dir, current_version="1.4.0", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.PENDING
    assert status.reviewed_through == "1.2.3"
    assert status.pending_target == "1.4.0"
    assert status.first_seen_at == _NOW


def test_observations_never_lower_review_or_pending_target(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)
    upgrade_review.observe(project_dir, current_version="1.3.0", now=_NOW)

    stale = upgrade_review.observe(project_dir, current_version="1.1.0", now=_NOW)

    assert stale.condition is upgrade_review.ReviewCondition.STALE_RUNTIME
    assert stale.pending_target == "1.3.0"
    assert _payload(project_dir)["pending_target"] == "1.3.0"


def test_concurrent_observers_preserve_highest_target(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda version: upgrade_review.observe(
                    project_dir, current_version=version, now=_NOW
                ),
                ["1.1.0", "1.5.0", "1.2.0", "1.4.0"],
            )
        )

    assert _payload(project_dir)["pending_target"] == "1.5.0"


def test_cross_process_observers_preserve_highest_target(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)
    script = (
        "from pathlib import Path; "
        "from booley.harness.upgrade_review import observe; "
        "import sys; observe(Path(sys.argv[1]), current_version=sys.argv[2])"
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    python_path = os.pathsep.join(
        part for part in (str(source_root), os.environ.get("PYTHONPATH", "")) if part
    )
    env = os.environ | {"PYTHONPATH": python_path}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(project_dir), version],
            env=env,
        )
        for version in ["1.1.0", "1.5.0", "1.2.0", "1.4.0"]
    ]

    assert [process.wait(timeout=10) for process in processes] == [0, 0, 0, 0]
    assert _payload(project_dir)["pending_target"] == "1.5.0"


def test_corrupt_state_is_preserved_for_diagnosis(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    path = upgrade_review.state_path(project_dir)
    path.write_bytes(b"{broken")

    status = upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.CORRUPT
    assert path.read_bytes() == b"{broken"


def test_non_utf8_state_is_preserved_for_diagnosis(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    path = upgrade_review.state_path(project_dir)
    path.write_bytes(b"\xff\xfe")

    status = upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.CORRUPT
    assert path.read_bytes() == b"\xff\xfe"


def test_unsupported_version_does_not_guess_or_write_state(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)

    status = upgrade_review.observe(project_dir, current_version="2.0.0rc1", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.UNSUPPORTED
    assert not upgrade_review.state_path(project_dir).exists()


def test_unwritable_runtime_shape_is_reported_fail_soft(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "runtime").write_text("not a directory", encoding="utf-8")

    status = upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)

    assert status.condition is upgrade_review.ReviewCondition.UNAVAILABLE


def test_acknowledgment_clears_exact_pending_target(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)
    upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)

    status = upgrade_review.acknowledge(
        project_dir,
        "2.0.0",
        current_version="2.0.0",
        packaged_changelog=_changelog(tmp_path),
    )

    assert status.condition is upgrade_review.ReviewCondition.CURRENT
    assert _payload(project_dir) == {"schema": 1, "reviewed_through": "2.0.0"}


def test_acknowledgment_rejects_stale_runtime_and_missing_entry(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)
    upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)

    with pytest.raises(upgrade_review.AcknowledgmentError, match="running Booley"):
        upgrade_review.acknowledge(
            project_dir,
            "2.0.0",
            current_version="1.0.0",
            packaged_changelog=_changelog(tmp_path),
        )
    with pytest.raises(upgrade_review.AcknowledgmentError, match="no release entry"):
        upgrade_review.acknowledge(
            project_dir,
            "2.0.0",
            current_version="2.0.0",
            packaged_changelog=_changelog(tmp_path, "1.9.0"),
        )
    assert _payload(project_dir)["pending_target"] == "2.0.0"


def test_newer_observation_wins_before_acknowledgment(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    upgrade_review.observe(project_dir, current_version="1.0.0", now=_NOW)
    upgrade_review.observe(project_dir, current_version="2.0.0", now=_NOW)
    upgrade_review.observe(project_dir, current_version="3.0.0", now=_NOW)

    with pytest.raises(upgrade_review.AcknowledgmentError, match="pending target changed"):
        upgrade_review.acknowledge(
            project_dir,
            "2.0.0",
            current_version="2.0.0",
            packaged_changelog=_changelog(tmp_path),
        )

    assert _payload(project_dir)["pending_target"] == "3.0.0"


def test_doctor_emits_stable_pending_finding(monkeypatch, tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    status = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.PENDING,
        "2.0.0",
        str(upgrade_review.state_path(project_dir)),
        "1.0.0",
        "2.0.0",
        _NOW,
    )
    monkeypatch.setattr(upgrade_review, "observe", lambda _project_dir: status)
    reporter = doctor._Reporter.create()

    doctor._check_upgrade_review(project_dir, reporter)

    assert reporter.findings is not None
    assert reporter.findings[0].check_id == "upgrade.review-pending"
    assert reporter.findings[0].severity == "warn"


def test_doctor_reports_current_review_as_pass(monkeypatch, tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    status = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.CURRENT,
        "2.0.0",
        str(upgrade_review.state_path(project_dir)),
        reviewed_through="2.0.0",
    )
    monkeypatch.setattr(upgrade_review, "observe", lambda _project_dir: status)
    reporter = doctor._Reporter.create()

    doctor._check_upgrade_review(project_dir, reporter)

    assert reporter.findings is not None
    assert reporter.findings[0].severity == "pass"


def test_doctor_emits_stable_stale_runtime_finding(monkeypatch, tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    status = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.STALE_RUNTIME,
        "1.0.0",
        str(upgrade_review.state_path(project_dir)),
        reviewed_through="1.0.0",
        pending_target="2.0.0",
    )
    monkeypatch.setattr(upgrade_review, "observe", lambda _project_dir: status)
    reporter = doctor._Reporter.create()

    doctor._check_upgrade_review(project_dir, reporter)

    assert reporter.findings is not None
    assert reporter.findings[0].check_id == "upgrade.runtime-stale"


def test_doctor_emits_stable_diagnostic_finding(monkeypatch, tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    status = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.CORRUPT,
        "2.0.0",
        str(upgrade_review.state_path(project_dir)),
        diagnostic="bad JSON",
    )
    monkeypatch.setattr(upgrade_review, "observe", lambda _project_dir: status)
    reporter = doctor._Reporter.create()

    doctor._check_upgrade_review(project_dir, reporter)

    assert reporter.findings is not None
    assert reporter.findings[0].check_id == "upgrade.review-state-corrupt"
