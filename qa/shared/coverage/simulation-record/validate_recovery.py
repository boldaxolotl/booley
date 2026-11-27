"""Validate retained corrupt-terminal recovery evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, raw


def validate_corrupt_terminal(
    archived_result_path: Path, restored_result_path: Path, rejection_path: Path
) -> None:
    """Require fail-closed corrupt-result rejection and byte-exact restoration."""
    _archived_document, archived = _load(archived_result_path)
    _restored_document, restored = _load(restored_result_path)
    _need(archived == restored, "restored result bytes differ from archived baseline")
    rejection, _ = _load(rejection_path)
    _need(rejection.get("exit_code") == 2, "corrupt resume did not exit 2")
    diagnostic = rejection.get("diagnostic")
    # Booley validates Results on read but does not re-hash them (ADR 0064), so the
    # diagnostic names the Simulation Result, not "integrity".
    _need(
        isinstance(diagnostic, str) and "simulation result" in diagnostic.lower(),
        "corrupt resume diagnostic does not name the Simulation Result",
    )
    attempts = rejection.get("attempts_before")
    _need(attempts == rejection.get("attempts_after"), "corrupt resume created an attempt")
    control = rejection.get("control_diagnostic")
    _need(
        rejection.get("control_exit_code") == 2
        and isinstance(control, str)
        and "booley flow sim" in control
        and "--coverage" in control,
        "restored control was not refused with a new --coverage run",
    )
    _need(
        attempts == rejection.get("attempts_after_restore"), "restored completed work item reran"
    )
    _need(rejection.get("eda_processes_started") == [], "corrupt resume launched EDA")
    valid_sha256 = _digest(archived)
    _need(rejection.get("valid_sha256") == valid_sha256, "archived result digest differs")
    _need(rejection.get("restored_sha256") == valid_sha256, "restored result digest differs")
    _need(rejection.get("corrupt_sha256") != valid_sha256, "corrupt result digest was unchanged")
