"""Cleanup refuses to mutate while a Job record cannot be read (#1931)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness.setup import cleanup
from booley.runtime import job_records


def test_unreadable_job_record_blocks_mutation_and_names_its_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_path = tmp_path / "run-1.json"
    record_path.write_text("{}", encoding="utf-8")

    def held(_run_id: str, _root: Path | None) -> None:
        raise PermissionError(13, "record held open")

    monkeypatch.setattr(job_records, "read_record", held)
    entry = SimpleNamespace(active=False, process_group_id=None, job_record=str(record_path))

    reason = cleanup._active_use_reason(entry)  # type: ignore[arg-type]

    assert reason is not None
    assert "Job record is unreadable" in reason
    assert "record held open" in reason
