"""Tests for the doctor freshness stamp (booley.runtime.doctor_stamp)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from booley.runtime import doctor_stamp
from booley.runtime.booley_dir import reset_cache


@pytest.fixture(autouse=True)
def isolate_project_cache():
    """Environment monkeypatch restoration must also release the resolver's cached Project."""
    reset_cache()
    yield
    reset_cache()


def _write_project(root: Path) -> Path:
    """Minimal project layout: booley.toml + seeded devcontainer.json."""
    booley_dir = root / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text('[project]\nname = "unit"\n', encoding="utf-8")
    dc_dir = root / ".devcontainer"
    dc_dir.mkdir()
    (dc_dir / "devcontainer.json").write_text('{"image": "booley-sandbox"}\n', encoding="utf-8")
    return booley_dir


class TestRecordAndLoad:
    def test_round_trip(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setattr(doctor_stamp.runtime_context, "inside_session_runtime", lambda: False)

        path = doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True)

        assert path == booley_dir / "runtime" / "doctor_stamp.json"
        stamp = doctor_stamp.load_stamp(booley_dir)
        assert stamp is not None
        assert stamp["deep"] is True
        assert stamp["runtime"] == "host"
        assert stamp["fingerprint"] == doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
        # Timestamp parses back in the format check_stamp expects.
        datetime.strptime(stamp["passed_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_records_session_runtime_venue(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setattr(doctor_stamp.runtime_context, "inside_session_runtime", lambda: True)

        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)

        assert doctor_stamp.load_stamp(booley_dir)["runtime"] == "session-runtime"

    def test_record_is_fail_soft_when_state_dir_unwritable(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        # A regular file where the runtime dir should be makes mkdir raise.
        (booley_dir / "runtime").write_text("", encoding="utf-8")

        assert doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False) is None

    def test_load_tolerates_garbage(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        assert doctor_stamp.load_stamp(booley_dir) is None  # absent
        path = doctor_stamp.stamp_path(booley_dir)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert doctor_stamp.load_stamp(booley_dir) is None
        path.write_text('["a", "list"]', encoding="utf-8")
        assert doctor_stamp.load_stamp(booley_dir) is None


class TestCheckStamp:
    def test_missing_stamp_nags(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)
        assert msg is not None
        assert "no warning-free `booley doctor` run is stamped" in msg
        assert "zero FAILs and zero active WARNs" in msg

    def test_fresh_matching_stamp_is_quiet(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)

        assert doctor_stamp.check_stamp(booley_dir, tmp_path) is None

    def test_stamp_from_different_booley_version_nags(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setattr("booley.__version__", "1.0.0")
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        monkeypatch.setattr("booley.__version__", "2.0.0")
        advisory = doctor_stamp.check_stamp_advisory(booley_dir, tmp_path)

        assert advisory is not None
        assert advisory.requires_action is True
        lines = advisory.message.splitlines()
        assert lines == [
            "=" * 72,
            "WARNING: Booley version changed from 1.0.0 to 2.0.0.",
            "ACTION REQUIRED: Invoke /booley-heal",
            "=" * 72,
        ]

    def test_stale_stamp_nags_with_age(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)

        later = datetime.now(tz=UTC) + timedelta(days=doctor_stamp.MAX_AGE_DAYS + 3)
        msg = doctor_stamp.check_stamp(booley_dir, tmp_path, now=later)

        assert msg is not None
        assert f"{doctor_stamp.MAX_AGE_DAYS + 3}d ago" in msg

    def test_edited_booley_toml_invalidates_stamp(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        before = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
        (booley_dir / "booley.toml").write_text('[project]\nname = "edited"\n', encoding="utf-8")
        after = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg
        assert f"booley_toml_sha256 {before['booley_toml_sha256']} -> " in msg
        assert str(after["booley_toml_sha256"]) in msg
        assert "doctor_waivers_sha256" not in msg
        assert "devcontainer_json_sha256" not in msg

    def test_regenerated_devcontainer_invalidates_stamp(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        before = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
        (tmp_path / ".devcontainer" / "devcontainer.json").write_text(
            '{"image": "rebuilt"}\n', encoding="utf-8"
        )
        after = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg
        assert f"devcontainer_json_sha256 {before['devcontainer_json_sha256']} -> " in msg
        assert str(after["devcontainer_json_sha256"]) in msg
        assert "booley_toml_sha256" not in msg
        assert "doctor_waivers_sha256" not in msg

    def test_edited_doctor_waivers_invalidates_stamp(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        (booley_dir / "doctor-waivers.toml").write_text("version = 1\n", encoding="utf-8")
        after = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "changed since the last clean" in msg
        assert "doctor_waivers_sha256 <none> -> " in msg
        assert str(after["doctor_waivers_sha256"]) in msg
        assert "booley_toml_sha256" not in msg
        assert "devcontainer_json_sha256" not in msg

    def test_malformed_fingerprint_does_not_echo_unknown_values(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        path = doctor_stamp.stamp_path(booley_dir)
        stamp = json.loads(path.read_text(encoding="utf-8"))
        stamp["fingerprint"] = {"OPENAI_API_KEY": "should-not-appear"}
        path.write_text(json.dumps(stamp), encoding="utf-8")

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "recorded_fingerprint <malformed> ->" in msg
        assert "OPENAI_API_KEY" not in msg
        assert "should-not-appear" not in msg

    def test_mismatch_wins_over_staleness(self, tmp_path):
        """Config drift is the stronger signal; report it even when also stale."""
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        (booley_dir / "booley.toml").write_text('[project]\nname = "edited"\n', encoding="utf-8")

        later = datetime.now(tz=UTC) + timedelta(days=30)
        msg = doctor_stamp.check_stamp(booley_dir, tmp_path, now=later)

        assert "changed since the last clean" in msg

    def test_unparsable_timestamp_nags(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        path = doctor_stamp.stamp_path(booley_dir)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"passed_at": "yesterday-ish"}), encoding="utf-8")

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "unreadable" in msg


class TestWarnIfStale:
    def test_emits_nag_for_unstamped_project(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
        reset_cache()
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)

        assert len(emitted) == 1
        assert "booley doctor" in emitted[0]

    def test_quiet_when_stamp_is_fresh(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
        reset_cache()
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)

        assert emitted == []

    def test_emphasizes_version_drift_at_emission_boundary(self, tmp_path, monkeypatch):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
        reset_cache()
        monkeypatch.setattr("booley.__version__", "1.0.0")
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        monkeypatch.setattr("booley.__version__", "2.0.0")
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(
            tmp_path,
            emitted.append,
            emphasize_action=lambda text: f"<bright-amber>{text}</bright-amber>",
        )

        assert emitted[0].startswith("<bright-amber>")
        assert emitted[0].endswith("</bright-amber>")

    def test_never_raises_when_booley_dir_unresolvable(self, tmp_path, monkeypatch):
        def boom(_start=None):
            raise FileNotFoundError("no project")

        monkeypatch.setattr(doctor_stamp, "resolve_booley_dir", boom)
        emitted: list[str] = []

        doctor_stamp.warn_if_stale(tmp_path, emitted.append)  # must not raise

        assert emitted == []


def test_stamp_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True)
    path = doctor_stamp.stamp_path(booley_dir)
    attempts = hold_opens(monkeypatch, {path: 1})
    stamp = doctor_stamp.load_stamp(booley_dir)
    assert stamp is not None
    assert stamp["deep"] is True
    assert attempts[path] == 2


def test_stamp_write_survives_transient_replace_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_replaces

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
    path = doctor_stamp.stamp_path(booley_dir)
    attempts = hold_replaces(monkeypatch, {path: 1})
    assert doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True) == path
    assert doctor_stamp.load_stamp(booley_dir)["deep"] is True
    assert attempts[path] == 2


def test_lasting_stamp_sharing_error_is_an_unreadable_advisory(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True)
    path = doctor_stamp.stamp_path(booley_dir)
    attempts = hold_opens(monkeypatch, {path: 1000})
    with pytest.raises(PermissionError) as lasting:
        doctor_stamp.load_stamp(booley_dir)
    assert lasting.value.winerror == 32  # type: ignore[attr-defined]
    advisory = doctor_stamp.check_stamp_advisory(booley_dir, tmp_path)
    assert advisory is not None
    assert "could not be read" in advisory.message
    assert "no warning-free" not in advisory.message
    assert attempts[path] > 2


def test_posix_unreadable_stamp_reads_as_absent(tmp_path, monkeypatch):
    import errno
    import sys
    from pathlib import Path

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True)
    monkeypatch.setattr(sys, "platform", "linux")

    def denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "open", denied)
    assert doctor_stamp.load_stamp(booley_dir) is None
    advisory = doctor_stamp.check_stamp_advisory(booley_dir, tmp_path)
    assert advisory is not None
    assert "no warning-free" in advisory.message


def test_stamp_write_lasting_sharing_error_stays_fail_soft(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_replaces

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
    path = doctor_stamp.stamp_path(booley_dir)
    hold_replaces(monkeypatch, {path: 1000})
    assert doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True) is None
    assert doctor_stamp.load_stamp(booley_dir)["deep"] is False
