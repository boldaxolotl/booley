"""Draft PRs skip heavy CI, but Mergify's speculative draft PRs never do.

Mergify validates queue candidates through draft PRs on
``mergify/merge-queue/`` branches. If the draft gate ever loses that
exemption, queue checks skip and ``ci-required`` stops proving anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from booley.core.boundary import require_dict

WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"

# Workflow file -> the job whose `if:` carries the draft gate.
GATED_JOBS = {
    "test.yml": "changes",
    "picorv32-demo.yml": "demo-contract",
    "pages.yml": "build",
}
DRAFT_GATE = (
    "github.event_name != 'pull_request' || !github.event.pull_request.draft"
    " || startsWith(github.head_ref, 'mergify/merge-queue/')"
)
PR_TYPES = ["opened", "synchronize", "reopened", "ready_for_review"]


def _load(name: str) -> dict[Any, Any]:
    parsed = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return require_dict(parsed, field=name)


def _triggers(workflow: dict[Any, Any], name: str) -> dict[Any, Any]:
    # PyYAML reads the bare `on:` key as boolean True.
    return require_dict(workflow[True], field=f"{name}.on")


@pytest.mark.parametrize(("name", "job"), sorted(GATED_JOBS.items()))
def test_draft_gate_exempts_mergify_queue_drafts(name: str, job: str) -> None:
    workflow = _load(name)

    assert workflow["jobs"][job]["if"] == DRAFT_GATE


@pytest.mark.parametrize("name", sorted(GATED_JOBS))
def test_marking_a_draft_ready_starts_ci(name: str) -> None:
    pull_request = _triggers(_load(name), name)["pull_request"]

    assert pull_request["types"] == PR_TYPES


def test_every_tests_job_skips_with_the_gated_changes_job() -> None:
    jobs = _load("test.yml")["jobs"]

    # ci-metrics hangs off ci-required, whose own `if` (pinned in
    # test_pytest_configuration.py) skips it when `changes` skips.
    for job_name, job in jobs.items():
        if job_name in {"changes", "ci-metrics"}:
            continue
        needs = job["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "changes" in needs, job_name
    assert jobs["ci-metrics"]["needs"] == "ci-required"


def test_mergify_never_accepts_a_skipped_ci_required() -> None:
    # A draft's skipped ci-required satisfies branch protection, so queueing
    # must keep demanding an explicit success.
    mergify = yaml.safe_load((WORKFLOWS.parents[1] / ".mergify.yml").read_text(encoding="utf-8"))
    rule = require_dict(mergify["queue_rules"][0], field="queue_rules[0]")

    for key in ("queue_conditions", "merge_conditions"):
        assert "check-success = @github-actions/ci-required" in rule[key], key
