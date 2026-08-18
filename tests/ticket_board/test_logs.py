"""Tests for booley.ticket_board.logs — log management."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.ticket_board.logs import (
    append_incident,
    clear_from_step,
    load_progress,
    progress_default,
    reset_progress,
    save_progress,
)


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# load_progress / save_progress
# ---------------------------------------------------------------------------


class TestLoadProgress:
    def test_returns_none_when_missing(self, logs_dir):
        assert load_progress(logs_dir, "my-ticket") is None

    def test_loads_existing_progress(self, logs_dir):
        slug_dir = logs_dir / "my-ticket"
        slug_dir.mkdir()
        data = {"step": "sim", "steps_completed": ["plan"]}
        (slug_dir / "progress.json").write_text(json.dumps(data), encoding="utf-8")
        result = load_progress(logs_dir, "my-ticket")
        assert result is not None
        assert result["step"] == "sim"
        assert result["steps_completed"] == ["plan"]

    def test_merges_with_defaults(self, logs_dir):
        slug_dir = logs_dir / "my-ticket"
        slug_dir.mkdir()
        (slug_dir / "progress.json").write_text('{"step": "lint"}', encoding="utf-8")
        result = load_progress(logs_dir, "my-ticket")
        assert result["failed_step"] is None
        assert result["error"] is None
        assert result["blocked_reason"] is None

    def test_returns_none_on_corrupt_json(self, logs_dir):
        slug_dir = logs_dir / "my-ticket"
        slug_dir.mkdir()
        (slug_dir / "progress.json").write_text("not json", encoding="utf-8")
        assert load_progress(logs_dir, "my-ticket") is None


class TestSaveProgress:
    def test_creates_dirs_and_writes(self, logs_dir):
        save_progress(logs_dir, "new-ticket", {"step": "plan"})
        path = logs_dir / "new-ticket" / ".runtime" / "progress.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["step"] == "plan"

    def test_overwrites_existing(self, logs_dir):
        save_progress(logs_dir, "t", {"step": "a"})
        save_progress(logs_dir, "t", {"step": "b"})
        data = json.loads(
            (logs_dir / "t" / ".runtime" / "progress.json").read_text(encoding="utf-8")
        )
        assert data["step"] == "b"


class TestResetProgress:
    def test_writes_defaults(self, logs_dir):
        save_progress(logs_dir, "t", {"step": "sim", "error": "boom"})
        reset_progress(logs_dir, "t")
        result = load_progress(logs_dir, "t")
        assert result["step"] == ""
        assert result["error"] is None
        assert result["steps_completed"] == []


class TestProgressDefault:
    def test_returns_deep_copy(self):
        val = progress_default("steps_completed")
        assert val == []
        val.append("x")
        assert progress_default("steps_completed") == []


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class TestAppendIncident:
    def test_creates_incidents_file(self, logs_dir):
        n = append_incident(logs_dir, "t", "timeout", "sim", "sim timed out")
        assert n == 1
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "# Incidents" in content
        assert "## Incident 1: timeout" in content
        assert "sim timed out" in content

    def test_increments_incident_number(self, logs_dir):
        append_incident(logs_dir, "t", "timeout", "sim", "first")
        n = append_incident(logs_dir, "t", "crash", "tb_coder", "second")
        assert n == 2
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "## Incident 2: crash" in content

    def test_includes_resolution(self, logs_dir):
        append_incident(logs_dir, "t", "err", "lint", "bad", resolution="fixed by retry")
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "fixed by retry" in content


# ---------------------------------------------------------------------------
# clear_from_step
# ---------------------------------------------------------------------------


class TestClearFromStep:
    def test_noop_when_log_dir_missing(self, logs_dir):
        clear_from_step(logs_dir, "nonexistent", "plan")

    def test_removes_status_json(self, logs_dir):
        slug_dir = logs_dir / "t"
        slug_dir.mkdir()
        status = slug_dir / "status.json"
        canonical_status = slug_dir / ".runtime" / "status.json"
        status.write_text("{}", encoding="utf-8")
        canonical_status.parent.mkdir()
        canonical_status.write_text("{}", encoding="utf-8")
        clear_from_step(logs_dir, "t", "plan")
        assert not status.exists()
        assert not canonical_status.exists()

    def test_appends_retry_banner(self, logs_dir):
        slug_dir = logs_dir / "t"
        slug_dir.mkdir()
        clear_from_step(logs_dir, "t", "plan")
        harness_log = slug_dir / "human-logs" / "harness.log"
        assert harness_log.exists()
        content = harness_log.read_text(encoding="utf-8")
        assert "RETRY from plan" in content
