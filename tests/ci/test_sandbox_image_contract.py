"""Mutation-backed tests for the parsed Runtime Image graph contract."""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.sandbox_image_contract import (
    ContractSources,
    dockerfile_parents,
    find_step,
    load_sources,
    logical_instructions,
    validate_sources,
)

_ROOT = Path(__file__).parents[2]


def _sources() -> ContractSources:
    return copy.deepcopy(load_sources(_ROOT))


def _errors(sources: ContractSources, role: str) -> str:
    errors = "\n".join(validate_sources(sources))
    assert role in errors, errors
    return errors


def _step(
    sources: ContractSources,
    workflow_field: str,
    workflow_name: str,
    job: str,
    name: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    workflow = getattr(sources, workflow_field)
    return find_step(workflow_name, workflow, job, name=name, step_id=step_id).value


def _remove_named_context(step: dict[str, Any], mapping: str) -> None:
    inputs = step.get("with")
    if isinstance(inputs, dict):
        inputs["build-contexts"] = str(inputs["build-contexts"]).replace(mapping, "")
        return
    step["run"] = str(step["run"]).replace(f"--build-context {mapping}", "")


def _sub_once(pattern: str, replacement: str, value: str) -> str:
    changed, count = re.subn(pattern, replacement, value, count=1)
    assert count == 1, pattern
    return changed


def test_repository_image_graph_and_runtime_roles_are_consistent() -> None:
    assert validate_sources(_sources()) == ()


def test_dockerfile_parser_uses_logical_lines_and_arg_substituted_from() -> None:
    dockerfile = """\
ARG PARENT=example.invalid/base:local
FROM ${PARENT}
RUN command --one \\
    && command --two
"""

    instructions = logical_instructions(dockerfile)

    assert dockerfile_parents(dockerfile) == ("example.invalid/base:local",)
    assert instructions[-1].keyword == "RUN"
    assert instructions[-1].value == "command --one && command --two"


def test_dockerfile_parser_rejects_unresolved_from_arg() -> None:
    with pytest.raises(ValueError, match="unresolved FROM"):
        dockerfile_parents("FROM ${MISSING}\n")


@pytest.mark.parametrize(
    ("role", "workflow_field", "workflow_name", "job", "name", "step_id", "mapping"),
    [
        (
            "local-base candidate",
            "test_workflow",
            "test.yml",
            "bwave-smoke",
            "Build candidate from changed stable base",
            None,
            "booley-runtime-base=docker-image://booley-runtime-base:ci",
        ),
        (
            "published-base test candidate",
            "test_workflow",
            "test.yml",
            "bwave-smoke",
            "Build candidate from published stable base",
            None,
            "booley-runtime-base=docker-image://${{ steps.runtime-base.outputs.image }}",
        ),
        (
            "release candidate",
            "release_workflow",
            "docker-publish.yml",
            "build-and-push",
            None,
            "build",
            "booley-runtime-base=docker-image://${{ steps.runtime-base.outputs.image }}",
        ),
        (
            "local RISC-V candidate",
            "test_workflow",
            "test.yml",
            "bwave-smoke",
            "Run RISC-V candidate image contract",
            None,
            "booley-sandbox=docker-image://booley-test",
        ),
        (
            "release RISC-V image",
            "release_workflow",
            "docker-publish.yml",
            "build-and-push-riscv",
            None,
            "build",
            "booley-sandbox=docker-image://${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@"
            "${{ needs.build-and-push.outputs.image-digest }}",
        ),
    ],
)
def test_each_symbolic_parent_requires_its_named_context(
    role: str,
    workflow_field: str,
    workflow_name: str,
    job: str,
    name: str | None,
    step_id: str | None,
    mapping: str,
) -> None:
    sources = _sources()
    step = _step(sources, workflow_field, workflow_name, job, name, step_id)
    _remove_named_context(step, mapping)

    errors = _errors(sources, role)

    assert "named contexts" in errors


@pytest.mark.parametrize("target", ["standard", "riscv"])
def test_release_derivations_reject_mutable_parent_tags(target: str) -> None:
    sources = _sources()
    job = "build-and-push" if target == "standard" else "build-and-push-riscv"
    step = _step(sources, "release_workflow", "docker-publish.yml", job, step_id="build")
    context = "booley-runtime-base" if target == "standard" else "booley-sandbox"
    step["with"]["build-contexts"] = f"{context}=docker-image://ghcr.io/acme/image:latest"

    role = "release candidate" if target == "standard" else "release RISC-V image"
    assert "named contexts" in _errors(sources, role)


@pytest.mark.parametrize("kind", ["action", "shell"])
def test_represented_builds_reject_intermediate_targets(kind: str) -> None:
    sources = _sources()
    if kind == "action":
        step = _step(
            sources, "release_workflow", "docker-publish.yml", "build-and-push", step_id="build"
        )
        step["with"]["target"] = "bwave-builder"
        role = "release candidate"
    else:
        step = _step(
            sources,
            "test_workflow",
            "test.yml",
            "bwave-smoke",
            "Build candidate from changed stable base",
        )
        step["run"] = str(step["run"]).replace(
            "docker buildx build", "docker buildx build --target bwave-builder", 1
        )
        role = "local-base candidate"

    assert "intermediate stage" in _errors(sources, role)


@pytest.mark.parametrize(
    ("step_name", "condition"),
    [
        ("Build changed stable runtime base locally", ""),
        (
            "Build candidate from published stable base",
            "steps.runtime-base.outputs.build == 'true'",
        ),
        ("Build candidate from changed stable base", "other.outputs.build == 'true'"),
        (
            "Build candidate from published stable base",
            "steps.runtime-base.outputs.build != 'false'",
        ),
    ],
)
def test_candidate_alternatives_reject_missing_or_overlapping_conditions(
    step_name: str, condition: str
) -> None:
    sources = _sources()
    step = _step(sources, "test_workflow", "test.yml", "bwave-smoke", step_name)
    step["if"] = condition

    assert "test candidate alternatives" in _errors(sources, "test candidate alternatives")


def test_candidate_alternatives_require_the_runtime_base_selector() -> None:
    sources = _sources()
    for name in (
        "Build changed stable runtime base locally",
        "Build candidate from changed stable base",
    ):
        step = _step(sources, "test_workflow", "test.yml", "bwave-smoke", name)
        step["if"] = "steps.other.outputs.build == 'true'"
    remote = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Build candidate from published stable base",
    )
    remote["if"] = "steps.other.outputs.build != 'true'"

    assert "runtime-base build selector" in _errors(sources, "test candidate alternatives")


def test_local_stable_base_must_precede_its_candidate() -> None:
    sources = _sources()
    steps = sources.test_workflow["jobs"]["bwave-smoke"]["steps"]
    base = next(
        step for step in steps if step.get("name") == "Build changed stable runtime base locally"
    )
    steps.remove(base)
    candidate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build candidate from changed stable base"
    )
    steps.insert(candidate_index + 1, base)

    assert "must precede" in _errors(sources, "test candidate alternatives")


def test_published_base_resolver_must_precede_its_candidate() -> None:
    sources = _sources()
    steps = sources.test_workflow["jobs"]["bwave-smoke"]["steps"]
    resolver = next(
        step for step in steps if step.get("name") == "Select compatible stable runtime base"
    )
    steps.remove(resolver)
    candidate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build candidate from published stable base"
    )
    steps.insert(candidate_index + 1, resolver)

    assert "must precede" in _errors(sources, "published-base test candidate")


def test_stable_base_publication_enforces_build_verify_promote_order() -> None:
    sources = _sources()
    steps = sources.base_workflow["jobs"]["build-publish-smoke"]["steps"]
    verify_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify exact published base compatibility and tools"
    )
    promote_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Promote verified base to main consumption tag"
    )
    steps[verify_index], steps[promote_index] = steps[promote_index], steps[verify_index]

    assert "out of order" in _errors(sources, "stable-base publish")


def test_stable_base_verification_requires_the_build_digest() -> None:
    sources = _sources()
    verify = _step(
        sources,
        "base_workflow",
        "docker-base-publish.yml",
        "build-publish-smoke",
        "Verify exact published base compatibility and tools",
    )
    verify["env"]["BASE_IMAGE"] = "ghcr.io/acme/base:latest"

    assert "build digest" in _errors(sources, "stable-base publish")


@pytest.mark.parametrize("job", ["build-and-push", "build-and-push-riscv"])
def test_release_jobs_must_export_their_build_digest(job: str) -> None:
    sources = _sources()
    sources.release_workflow["jobs"][job]["outputs"]["image-digest"] = (
        "${{ steps.version.outputs.version }}"
    )

    role = "release candidate" if job == "build-and-push" else "release RISC-V image"
    assert "image-digest" in _errors(sources, role)


def test_release_riscv_job_must_depend_on_candidate_producer() -> None:
    sources = _sources()
    sources.release_workflow["jobs"]["build-and-push-riscv"]["needs"] = []

    assert "must depend" in _errors(sources, "release RISC-V image")


@pytest.mark.parametrize("job", ["build-and-push", "build-and-push-riscv"])
def test_release_parent_provenance_must_declare_a_registry_digest(job: str) -> None:
    sources = _sources()
    step = _step(sources, "release_workflow", "docker-publish.yml", job, step_id="build")
    step["with"]["labels"] = str(step["with"]["labels"]).replace(
        "io.booley.build.parent-artifact-kind=registry-digest",
        "io.booley.build.parent-artifact-kind=registry-tag",
    )

    role = "release candidate" if job == "build-and-push" else "release RISC-V image"
    assert "parent artifact kind" in _errors(sources, role)


@pytest.mark.parametrize(
    ("workflow_field", "workflow_name", "job", "name", "role"),
    [
        (
            "test_workflow",
            "test.yml",
            "bwave-smoke",
            "Select compatible stable runtime base",
            "published-base test candidate",
        ),
        (
            "release_workflow",
            "docker-publish.yml",
            "build-and-push",
            "Resolve compatible stable runtime base",
            "release candidate",
        ),
        (
            "base_workflow",
            "docker-base-publish.yml",
            "build-publish-smoke",
            "Verify exact published base compatibility and tools",
            "stable-base publish",
        ),
    ],
)
def test_current_workflow_callers_require_remote_resolution(
    workflow_field: str, workflow_name: str, job: str, name: str, role: str
) -> None:
    sources = _sources()
    step = _step(sources, workflow_field, workflow_name, job, name)
    step["run"] = str(step["run"]).replace("--resolver remote", "--resolver shadow")

    assert "remote stable-base resolver" in _errors(sources, role)


def test_resolver_contract_ignores_dead_comment_text() -> None:
    sources = _sources()
    step = _step(
        sources,
        "release_workflow",
        "docker-publish.yml",
        "build-and-push",
        "Resolve compatible stable runtime base",
    )
    step["run"] = str(step["run"]).replace("--resolver remote", "--resolver shadow")
    step["run"] += "\n# --resolver remote"

    assert "remote stable-base resolver" in _errors(sources, "release candidate")


@pytest.mark.parametrize(
    ("mutation", "role", "left", "right"),
    [
        ("node-authority", "Node", "Dockerfile.base", "agent_policy_probe.py"),
        ("node-probe", "Node", "Dockerfile.base", "agent_policy_probe.py"),
        ("cocotb-authority", "Cocotb", "Dockerfile.base", "cocotb-config"),
        ("cocotb-build-probe", "Cocotb", "Dockerfile.base", "cocotb-config"),
        ("cocotb-production-probe", "Cocotb", "Dockerfile.base", "test.yml"),
    ],
)
def test_runtime_role_mutations_name_both_disagreeing_sources(
    mutation: str, role: str, left: str, right: str
) -> None:
    sources = _mutate_runtime_role(_sources(), mutation)

    errors = _errors(sources, role)

    assert left in errors
    assert right in errors


def _mutate_runtime_role(sources: ContractSources, mutation: str) -> ContractSources:
    if mutation.startswith("node-"):
        return _mutate_node_role(sources, mutation)
    return _mutate_cocotb_role(sources, mutation)


def _mutate_node_role(sources: ContractSources, mutation: str) -> ContractSources:
    if mutation == "node-authority":
        contents = _sub_once(
            r"ARG NODE_VERSION=[^\s]+", "ARG NODE_VERSION=99.0.0", sources.base_dockerfile
        )
        return replace(sources, base_dockerfile=contents)
    step = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Run installed agent CLI policy probe",
    )
    step["run"] = _sub_once(r"--expected-node\s+[^\s]+", "--expected-node 99.0.0", step["run"])
    return sources


def _mutate_cocotb_role(sources: ContractSources, mutation: str) -> ContractSources:
    if mutation == "cocotb-authority":
        return replace(
            sources,
            base_dockerfile=_sub_once(
                r'(["\']cocotb==)[^"\']+', r"\g<1>99.0.0", sources.base_dockerfile
            ),
        )
    if mutation == "cocotb-build-probe":
        return replace(
            sources,
            base_dockerfile=_sub_once(
                r'(cocotb-config --version\)"\s*=\s*")[^"]+',
                r"\g<1>99.0.0",
                sources.base_dockerfile,
            ),
        )
    step = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Run cocotb Icarus/Verilator production-image flows",
    )
    step["run"] = _sub_once(
        r'(cocotb-config --version\)\\?"\s*=\s*)[0-9.]+',
        r"\g<1>99.0.0",
        str(step["run"]),
    )
    return sources


def test_cocotb_production_probe_requires_candidate_image() -> None:
    sources = _sources()
    step = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Run cocotb Icarus/Verilator production-image flows",
    )
    step["run"] = str(step["run"]).replace("booley-test bash -c", "other-image bash -c")

    assert "production probe image" in _errors(sources, "Cocotb")


@pytest.mark.parametrize("target", ["sim_icarus", "sim_verilator"])
def test_cocotb_production_probe_requires_both_simulation_flows(target: str) -> None:
    sources = _sources()
    step = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Run cocotb Icarus/Verilator production-image flows",
    )
    command = f"python3 -m booley.flows.sim --work-dir /validation-tmp/project --target {target}"
    step["run"] = str(step["run"]).replace(command, "true")

    assert f"missing {target} Simulation" in _errors(sources, "Cocotb")


def test_runtime_authorities_reject_ranges_and_duplicates() -> None:
    sources = _sources()
    ranged = replace(
        sources,
        base_dockerfile=_sub_once(r"cocotb==", "cocotb>=", sources.base_dockerfile),
    )
    node_line = re.search(r"^ARG NODE_VERSION=[^\s]+$", sources.base_dockerfile, re.MULTILINE)
    assert node_line is not None
    duplicated = replace(
        sources,
        base_dockerfile=sources.base_dockerfile.replace(
            node_line.group(0), f"{node_line.group(0)}\n{node_line.group(0)}", 1
        ),
    )

    assert "expected one exact version" in _errors(ranged, "Cocotb authority")
    assert "expected one exact version" in _errors(duplicated, "Node authority")
