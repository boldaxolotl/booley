from __future__ import annotations

import json

from booley.ticket_board.paths import (
    existing_human_log_file,
    existing_runtime_file,
    migrate_runtime_file,
    runtime_file,
)
from booley.ticket_board.validation import validate_logs


def test_existing_helpers_fall_back_to_legacy_until_migrated(tmp_path):
    logs_dir = tmp_path / "logs"
    legacy_state = logs_dir / "slug" / "booley_state.json"
    legacy_run = logs_dir / "slug" / "run.log"
    legacy_state.parent.mkdir(parents=True)
    legacy_state.write_text("{}", encoding="utf-8")
    legacy_run.write_text("old", encoding="utf-8")

    assert existing_runtime_file(logs_dir, "slug", "booley_state.json") == legacy_state
    assert existing_human_log_file(logs_dir, "slug", "run.log") == legacy_run

    new_state = migrate_runtime_file(logs_dir / "slug", "booley_state.json")

    assert new_state == runtime_file(logs_dir, "slug", "booley_state.json")
    assert new_state.exists()
    assert not legacy_state.exists()
    # legacy run.log is untouched: no production caller migrates human logs.
    assert legacy_run.exists()


def test_validate_logs_checks_current_runtime_artifacts(tmp_path):
    logs_dir = tmp_path / "logs"
    runtime = logs_dir / "slug" / ".runtime"
    human = logs_dir / "slug" / "human-logs"
    runtime.mkdir(parents=True)
    human.mkdir()
    (runtime / "progress.json").write_text("{}", encoding="utf-8")
    (human / "transitions.log").write_text("", encoding="utf-8")
    (runtime / "booley_state.json").write_text(
        json.dumps(
            {
                "slug": "slug",
                "criteria": {
                    "sim_pass_default": {"met": True, "mandatory": True},
                    "_report_submitted": {"met": True, "mandatory": False},
                },
            }
        ),
        encoding="utf-8",
    )

    result = validate_logs(logs_dir, "slug", "bugfix", ["setup", "summary"])

    assert result["missing_files"] == []
    assert result["skipped_steps"] == []
    assert result["gate_failures"] == []


def test_validate_logs_flags_empty_slug_and_optional_only_criteria(tmp_path):
    logs_dir = tmp_path / "logs"
    runtime = logs_dir / "slug" / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "progress.json").write_text("{}", encoding="utf-8")
    (runtime / "booley_state.json").write_text(
        json.dumps(
            {
                "slug": "",
                "criteria": {
                    "sim_pass_default": {"met": True, "mandatory": False},
                },
            }
        ),
        encoding="utf-8",
    )

    result = validate_logs(logs_dir, "slug", "bugfix", ["setup", "summary"])

    messages = {item["message"] for item in result["gate_failures"]}
    assert "booley_state.json has empty slug" in messages
    assert "booley_state.json has visible criteria but none are mandatory" in messages
