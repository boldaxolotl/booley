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
        "workflow.publication-topology",
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


def test_publication_topology_grants_reusable_workflow_permissions() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    publication["jobs"]["source-validation"]["permissions"].pop("actions")

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert errors == ("source-validation must grant actions: read required by test.yml",)


def test_publication_topology_requires_official_release_attestation() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    build = next(
        step
        for step in publication["jobs"]["build-package"]["steps"]
        if step.get("name") == "Build sdist and wheel"
    )
    build["run"] = build["run"].replace(", profile=BuildProfile.OFFICIAL_RELEASE", "")

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert "build-package must attest the official release wheel" in errors


def test_publication_attestation_must_be_in_post_tag_build_step() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    steps = publication["jobs"]["build-package"]["steps"]
    tag_check = next(
        step for step in steps if step.get("name") == "Verify tag matches package version"
    )
    build = next(step for step in steps if step.get("name") == "Build sdist and wheel")
    attestation = "write_build_stamp(Path.cwd(), profile=BuildProfile.OFFICIAL_RELEASE)"
    build["run"] = build["run"].replace(attestation, "write_build_stamp(Path.cwd())")
    tag_check["run"] += f"\n# {attestation}"

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert "build-package must attest the official release wheel" in errors


def test_publication_topology_requires_clean_wheel_staging() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    build = next(
        step
        for step in publication["jobs"]["build-package"]["steps"]
        if step.get("name") == "Build sdist and wheel"
    )
    build["run"] = build["run"].replace("rm -rf build/", "true")

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert "build-package must clean stale wheel staging" in errors


def test_publication_topology_verifies_attestation_in_both_artifact_paths() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    direct = publication["jobs"]["test-wheel"]["steps"]
    direct[:] = [step for step in direct if step.get("name") != "Verify release attestation"]
    rebuilt = next(
        step
        for step in publication["jobs"]["test-sdist"]["steps"]
        if step.get("name") == "Build and install from the exact source distribution"
    )
    rebuilt["run"] = rebuilt["run"].replace(
        "assert embedded_official_release() is True",
        "assert True",
    )

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert "test-wheel must verify the official release attestation" in errors
    assert "test-sdist must verify the official release attestation" in errors


def test_publication_topology_verifies_development_context_is_absent() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    for job_name in ("test-wheel", "test-sdist"):
        for step in publication["jobs"][job_name]["steps"]:
            if "embedded_development_context_path().exists()" in str(step.get("run", "")):
                step["run"] = step["run"].replace(
                    "embedded_development_context_path().exists()",
                    "False",
                )

    errors = semantic.validate_publication_topology(publication, _test_workflow())

    assert "test-wheel must verify the official release attestation" in errors
    assert "test-sdist must verify the official release attestation" in errors


def test_runtime_image_workflow_does_not_attest_a_pypi_release_wheel() -> None:
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert "BuildProfile.OFFICIAL_RELEASE" not in workflow
    assert "profile=BuildProfile.RUNTIME_IMAGE" in workflow


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


def test_release_topology_requires_real_picorv32_flows_and_evidence() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["picorv32-demo-flows"]
    flow = next(
        step for step in job["steps"] if step.get("name") == "Run exact reviewed demo flows"
    )
    flow["env"]["BOOLEY_RUN_PICORV32_FLOWS"] = "0"
    upload = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    upload["with"]["if-no-files-found"] = "warn"

    errors = semantic.validate_release_topology(workflow)

    assert "picorv32-demo-flows must enable lint and simulation" in errors
    assert "picorv32-demo-flows must reject missing evidence" in errors


def test_release_topology_requires_shared_provenance_validation() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )
    standard = workflow["jobs"]["standard-image-contract"]
    validation = next(
        step
        for step in standard["steps"]
        if step.get("name") == "Validate provenance, runtime, size, and resources"
    )
    validation["run"] = validation["run"].replace(
        "release_validation/image_provenance.py", "release_validation/missing.py"
    )

    assert "standard-image-contract must validate shared provenance and SBOM evidence" in (
        semantic.validate_release_topology(workflow)
    )
