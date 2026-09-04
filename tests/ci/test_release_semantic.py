from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from release_validation import semantic


def _test_workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))


def test_repository_release_semantics_are_valid() -> None:
    evidence = semantic.validate_repository(ROOT, candidate_sha="candidate-sha")

    assert evidence["schema"] == 1
    assert evidence["candidate_sha"] == "candidate-sha"
    assert evidence["errors"] == []
    assert {check["id"] for check in evidence["checks"]} >= {
        "classifier.release-sensitive",
        "workflow.pr-topology",
        "workflow.release-topology",
    }
    assert all(check["status"] == "pass" for check in evidence["checks"])


def test_pr_topology_requires_fast_semantic_job_in_stable_aggregate() -> None:
    workflow = _test_workflow()
    workflow["jobs"]["ci-required"]["needs"].remove("release-semantic")

    errors = semantic.validate_pr_topology(workflow)

    assert errors == ("ci-required must depend on release-semantic",)


def test_pr_topology_requires_one_minute_semantic_budget() -> None:
    workflow = _test_workflow()
    semantic_job = workflow["jobs"]["release-semantic"]
    budget = next(step for step in semantic_job["steps"] if step.get("name") == "Enforce budget")
    budget["run"] = budget["run"].replace("--budget-seconds 60", "--budget-seconds 120")

    errors = semantic.validate_pr_topology(workflow)

    assert errors == ("release-semantic must enforce a 60-second duration budget",)
