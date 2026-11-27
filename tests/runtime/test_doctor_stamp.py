"""Tests for the doctor freshness stamp (booley.runtime.doctor_stamp)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from booley.runtime import doctor_stamp


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
    @pytest.mark.parametrize(("container", "venue"), [("0", "host"), ("1", "session-runtime")])
    def test_round_trip_records_venue(
        self, tmp_path, monkeypatch, hand_written_sandbox, container, venue
    ):
        booley_dir = _write_project(tmp_path)
        monkeypatch.setenv("BOOLEY_CONTAINER", container)

        path = doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True)

        assert path == booley_dir / "runtime" / "doctor_stamp.json"
        stamp = doctor_stamp.load_stamp(booley_dir)
        assert stamp is not None
        assert stamp["deep"] is True
        assert stamp["runtime"] == venue
        assert stamp["fingerprint"] == doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
        # Timestamp parses back in the format check_stamp expects.
        datetime.strptime(stamp["passed_at"], "%Y-%m-%dT%H:%M:%SZ")

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

    @pytest.mark.parametrize(
        ("relative", "text", "key"),
        [
            (".booley/booley.toml", '[project]\nname = "edited"\n', "booley_toml_sha256"),
            (".devcontainer/devcontainer.json", '{"image": "new"}\n', "devcontainer_json_sha256"),
            (".booley/doctor-waivers.toml", "version = 1\n", "doctor_waivers_sha256"),
        ],
        ids=["booley-toml", "devcontainer", "waivers"],
    )
    def test_edited_input_invalidates_stamp(self, tmp_path, relative, text, key):
        booley_dir = _write_project(tmp_path)
        doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
        before = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
        (tmp_path / relative).write_text(text, encoding="utf-8")
        after = doctor_stamp.compute_fingerprint(booley_dir, tmp_path)

        # Also stale: config drift is the stronger signal and is the one reported.
        later = datetime.now(tz=UTC) + timedelta(days=doctor_stamp.MAX_AGE_DAYS + 3)
        msg = doctor_stamp.check_stamp(booley_dir, tmp_path, now=later)

        assert msg is not None
        assert "changed since the last clean" in msg
        assert f"{key} {before.get(key) or '<none>'} -> " in msg
        assert str(after[key]) in msg
        others = {"booley_toml_sha256", "devcontainer_json_sha256", "doctor_waivers_sha256"}
        assert not [other for other in others - {key} if other in msg]

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

    def test_unparsable_timestamp_nags(self, tmp_path):
        booley_dir = _write_project(tmp_path)
        path = doctor_stamp.stamp_path(booley_dir)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"passed_at": "yesterday-ish"}), encoding="utf-8")

        msg = doctor_stamp.check_stamp(booley_dir, tmp_path)

        assert msg is not None
        assert "unreadable" in msg


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


@pytest.mark.parametrize(
    ("holds", "written"), [(1, True), (1000, False)], ids=["brief", "lasting"]
)
def test_stamp_write_sharing_error(tmp_path, monkeypatch, holds, written):
    """A brief sharing violation is retried; a lasting one stays fail-soft."""
    from tests.sharing_error_support import hold_replaces

    booley_dir = _write_project(tmp_path)
    doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=False)
    path = doctor_stamp.stamp_path(booley_dir)
    attempts = hold_replaces(monkeypatch, {path: holds})
    assert doctor_stamp.record_clean_run(booley_dir, tmp_path, deep=True) == (
        path if written else None
    )
    assert doctor_stamp.load_stamp(booley_dir)["deep"] is written
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
