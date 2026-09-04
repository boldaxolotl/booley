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


def test_release_topology_splits_validation_by_image_dependency() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )

    assert semantic.validate_release_topology(workflow) == ()

    jobs = workflow["jobs"]
    validation_jobs = {
        "standard-image-contract",
        "openroad-runtime",
        "host-doctor-runtime",
        "simulation-selftest-overlay",
        "helper-image-metadata",
        "riscv-image-contract",
        "demo-ticket-surface",
        "picorv32-demo-flows",
        "ibex-lint-demo",
    }
    assert validation_jobs <= set(jobs)
    assert "demo-smoke" not in jobs
    assert set(jobs["promote"]["needs"]) == {
        "build-and-push",
        "build-and-push-riscv",
        *validation_jobs,
    }


def test_release_topology_rejects_riscv_output_reference_in_standard_job() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )
    workflow["jobs"]["openroad-runtime"]["steps"][0]["name"] = (
        "${{ needs.build-and-push-riscv.outputs.image-digest }}"
    )

    assert semantic.validate_release_topology(workflow) == (
        "standard release job openroad-runtime references the RISC-V build output",
    )
