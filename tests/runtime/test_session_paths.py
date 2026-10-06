"""Session runtime layout shared by every execution mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import session_paths
from booley.ticket_board import paths as ticket_paths


def test_explicit_root_names_its_jobs_directory_without_reading_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "session-logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "session-runtime"))

    assert session_paths.session_jobs_dir(tmp_path / "goal" / ".runtime") == (
        tmp_path / "goal" / ".runtime" / "jobs"
    )


def test_configured_runtime_dir_wins_over_logs_derived_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert session_paths.session_jobs_dir() == tmp_path / "runtime" / "jobs"


def test_logs_dir_alone_derives_the_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)

    assert session_paths.session_jobs_dir() == tmp_path / "logs" / ".runtime" / "jobs"


def test_unset_logs_dir_disables_job_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", "/ignored")

    assert session_paths.session_jobs_dir() is None


def test_ticket_paths_keep_the_shared_layout(tmp_path: Path) -> None:
    assert ticket_paths.session_jobs_dir is session_paths.session_jobs_dir
    assert ticket_paths.RUNTIME_DIR == session_paths.RUNTIME_DIR
    assert ticket_paths.ticket_runtime_dir(tmp_path) == session_paths.logs_runtime_dir(tmp_path)
