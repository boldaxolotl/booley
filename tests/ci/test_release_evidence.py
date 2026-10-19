from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import evidence


def test_report_binds_checks_and_runtime_state_to_candidate() -> None:
    payload = evidence.report(
        candidate_sha="candidate-sha",
        images={"sandbox": "sha256:image"},
        check_ids=["demo.lint", "demo.simulation"],
        uid=1000,
        gid=1000,
        cleanup=["container_removed", "workspace_ownership_restored"],
    )

    assert payload == {
        "schema": 1,
        "candidate": {
            "sha": "candidate-sha",
            "images": {"sandbox": "sha256:image"},
        },
        "checks": [
            {"id": "demo.lint", "status": "pass"},
            {"id": "demo.simulation", "status": "pass"},
        ],
        "identity": {"uid": 1000, "gid": 1000},
        "cleanup": {
            "container_removed": True,
            "workspace_ownership_restored": True,
        },
    }


def test_report_rejects_ambiguous_or_incomplete_evidence() -> None:
    with pytest.raises(ValueError, match="image identity"):
        evidence.report(candidate_sha="sha", images={}, check_ids=["check"])
    with pytest.raises(ValueError, match="unique"):
        evidence.report(
            candidate_sha="sha",
            images={"sandbox": "digest"},
            check_ids=["same", "same"],
        )
    with pytest.raises(ValueError, match="provided together"):
        evidence.report(
            candidate_sha="sha",
            images={"sandbox": "digest"},
            check_ids=["check"],
            uid=1000,
        )
