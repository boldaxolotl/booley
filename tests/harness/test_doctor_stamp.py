"""Tests for the doctor freshness stamp (booley.harness.doctor_stamp)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from booley.harness import doctor_stamp
from booley.runtime.project_dir import reset_cache


def _write_project(root: Path) -> Path:
    """Minimal project layout: booley.toml + seeded devcontainer.json."""
    project_dir = root / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text('[project]\nname = "unit"\n', encoding="utf-8")
    dc_dir = root / ".devcontainer"
    dc_dir.mkdir()
    (dc_dir / "devcontainer.json").write_text('{"image": "booley-sandbox"}\n', encoding="utf-8")
    return project_dir


class TestRecordAndLoad:
    def test_round_trip(self, tmp_path, monkeypatch):
        project_dir = _write_project(tmp_path)
        monkeypatch.setattr(doctor_stamp.runtime_context, "inside_session_runtime", lambda: False)

        path = doctor_stamp.record_clean_run(project_dir, tmp_path, deep=True)

        assert path == project_dir / "runtime" / "doctor_stamp.json"
        stamp = doctor_stamp.load_stamp(project_dir)
        assert stamp is not None
        assert stamp["deep"] is True
        assert stamp["runtime"] == "host"
        assert stamp["fingerprint"] == doctor_stamp.compute_fingerprint(project_dir, tmp_path)
        # Timestamp parses back in the format check_stamp expects.
        datetime.strptime(stamp["passed_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_records_session_runtime_venue(self, tmp_path, monkeypatch):
        project_dir = _write_project(tmp_path)
        monkeypatch.setattr(doctor_stamp.runtime_context, "inside_session_runtime", lambda: True)

        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)

        assert doctor_stamp.load_stamp(project_dir)["runtime"] == "session-runtime"

    def test_record_is_fail_soft_when_state_dir_unwritable(self, tmp_path):
        project_dir = _write_project(tmp_path)
        # A regular file where the runtime dir should be makes mkdir raise.
        (project_dir / "runtime").write_text("", encoding="utf-8")

        assert doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False) is None

    def test_load_tolerates_garbage(self, tmp_path):
        project_dir = _write_project(tmp_path)
        assert doctor_stamp.load_stamp(project_dir) is None  # absent
        path = doctor_stamp.stamp_path(project_dir)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert doctor_stamp.load_stamp(project_dir) is None
        path.write_text('["a", "list"]', encoding="utf-8")
        assert doctor_stamp.load_stamp(project_dir) is None


class TestCheckStamp:
    def test_missing_stamp_nags(self, tmp_path):
        project_dir = _write_project(tmp_path)
        msg = doctor_stamp.check_stamp(project_dir, tmp_path)
        assert msg is not None
        assert "no warning-free `booley doctor` run is stamped" in msg
        assert "zero FAILs and zero active WARNs" in msg

    def test_fresh_matching_stamp_is_quiet(self, tmp_path):
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)

        assert doctor_stamp.check_stamp(project_dir, tmp_path) is None

    def test_stamp_from_different_booley_version_nags(self, tmp_path, monkeypatch):
        project_dir = _write_project(tmp_path)
        monkeypatch.setattr("booley.__version__", "1.0.0")
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        monkeypatch.setattr("booley.__version__", "2.0.0")
        monkeypatch.setattr(
            doctor_stamp.colors,
            "bold_amber",
            lambda text: f"<bright-amber>{text}</bright-amber>",
        )

        msg = doctor_stamp.check_stamp(project_dir, tmp_path)

        assert msg is not None
        assert msg.startswith("<bright-amber>")
        assert msg.endswith("</bright-amber>")
        lines = msg.removeprefix("<bright-amber>").removesuffix("</bright-amber>").splitlines()
        assert lines == [
            "=" * 72,
            "WARNING: Booley version changed from 1.0.0 to 2.0.0.",
            "ACTION REQUIRED: Invoke /booley-heal",
            "=" * 72,
        ]

    def test_stale_stamp_nags_with_age(self, tmp_path):
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)

        later = datetime.now(tz=UTC) + timedelta(days=doctor_stamp.MAX_AGE_DAYS + 3)
        msg = doctor_stamp.check_stamp(project_dir, tmp_path, now=later)

        assert msg is not None
        assert f"{doctor_stamp.MAX_AGE_DAYS + 3} days ago" in msg

    def test_edited_booley_toml_invalidates_stamp(self, tmp_path):
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        (project_dir / "booley.toml").write_text('[project]\nname = "edited"\n', encoding="utf-8")

        msg = doctor_stamp.check_stamp(project_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg

    def test_regenerated_devcontainer_invalidates_stamp(self, tmp_path):
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        (tmp_path / ".devcontainer" / "devcontainer.json").write_text(
            '{"image": "rebuilt"}\n', encoding="utf-8"
        )

        msg = doctor_stamp.check_stamp(project_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg

    def test_edited_doctor_waivers_invalidates_stamp(self, tmp_path):
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        (project_dir / "doctor-waivers.toml").write_text("version = 1\n", encoding="utf-8")

        msg = doctor_stamp.check_stamp(project_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg

    def test_mismatch_wins_over_staleness(self, tmp_path):
        """Config drift is the stronger signal; report it even when also stale."""
        project_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        (project_dir / "booley.toml").write_text('[project]\nname = "edited"\n', encoding="utf-8")

        later = datetime.now(tz=UTC) + timedelta(days=30)
        msg = doctor_stamp.check_stamp(project_dir, tmp_path, now=later)

        assert "changed since the last clean" in msg

    def test_unparsable_timestamp_nags(self, tmp_path):
        project_dir = _write_project(tmp_path)
        path = doctor_stamp.stamp_path(project_dir)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"passed_at": "yesterday-ish"}), encoding="utf-8")

        msg = doctor_stamp.check_stamp(project_dir, tmp_path)

        assert msg is not None
        assert "unreadable" in msg


class TestWarnIfStale:
    def test_emits_nag_for_unstamped_project(self, tmp_path, monkeypatch):
        project_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        reset_cache()
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)

        assert len(emitted) == 1
        assert "booley doctor" in emitted[0]

    def test_quiet_when_stamp_is_fresh(self, tmp_path, monkeypatch):
        project_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        reset_cache()
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)

        assert emitted == []

    def test_never_raises_when_project_dir_unresolvable(self, tmp_path, monkeypatch):
        def boom(_start=None):
            raise FileNotFoundError("no project")

        monkeypatch.setattr(doctor_stamp, "resolve_project_dir", boom)
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)  # must not raise

        assert emitted == []
