"""The corrupt-terminal validator accepts the Simulation Result diagnostic Booley prints."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "qa_coverage_validate_recovery",
    Path(__file__).resolve().parents[2]
    / "qa/shared/coverage/simulation-record/validate_recovery.py",
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def _evidence(
    tmp_path: Path, diagnostic: str, control_exit_code: int = 2
) -> tuple[Path, Path, Path]:
    """Write archived and restored Results plus a rejection record with *diagnostic*."""
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"grade": "pass"}))
    digest = "sha256:" + hashlib.sha256(result.read_bytes()).hexdigest()
    attempts = ["attempt-1"]
    rejection = tmp_path / "rejection.json"
    rejection.write_text(
        json.dumps(
            {
                "exit_code": 2,
                "diagnostic": diagnostic,
                "attempts_before": attempts,
                "attempts_after": attempts,
                "attempts_after_restore": attempts,
                "control_exit_code": control_exit_code,
                "control_diagnostic": "start a new run:\n  booley flow sim --target t --coverage",
                "eda_processes_started": [],
                "valid_sha256": digest,
                "restored_sha256": digest,
                "corrupt_sha256": "sha256:" + "0" * 64,
            }
        )
    )
    return result, result, rejection


def test_accepts_a_simulation_result_validation_diagnostic(tmp_path: Path) -> None:
    recovery.validate_corrupt_terminal(*_evidence(tmp_path, "Simulation Result grade is invalid"))


def test_rejects_a_diagnostic_that_does_not_name_the_simulation_result(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not name the Simulation Result"):
        recovery.validate_corrupt_terminal(*_evidence(tmp_path, "start a new run instead"))


def test_rejects_a_restored_control_that_resumed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="restored control was not refused"):
        recovery.validate_corrupt_terminal(
            *_evidence(tmp_path, "Simulation Result grade is invalid", control_exit_code=0)
        )
