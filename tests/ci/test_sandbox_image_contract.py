"""Mutation-backed tests for the parsed Sandbox Image graph contract."""

from __future__ import annotations

import copy
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.sandbox_image_contract import (
    ContractSources,
    find_step,
    load_sources,
    validate_sources,
)
from tests.subprocess_budget import run_bounded_child

from booley.runtime import dockerfile_syntax
from booley.runtime.dockerfile_syntax import logical_instructions

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

    references = dockerfile_syntax.from_references(dockerfile)
    assert tuple(reference.image for reference in references) == ("example.invalid/base:local",)
    assert instructions[-1].keyword == "RUN"
    assert instructions[-1].value == "command --one && command --two"


def test_dockerfile_parser_rejects_unresolved_from_arg() -> None:
    with pytest.raises(ValueError, match="unresolved FROM"):
        dockerfile_syntax.from_references("FROM ${MISSING}\n")


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
            "booley-substrate=docker-image://${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}@"
            "${{ steps.substrate.outputs.digest }}",
        ),
        (
            "published-base test candidate",
            "test_workflow",
            "test.yml",
            "riscv-image",
            "Build candidate from published stable base",
            None,
            "booley-runtime-base=docker-image://${{ steps.runtime-base.outputs.image }}",
        ),
        (
            "local-base candidate",
            "test_workflow",
            "test.yml",
            "riscv-image",
            "Build candidate from changed stable base",
            None,
            "booley-runtime-base=docker-image://booley-runtime-base:ci",
        ),
        (
            "local RISC-V candidate",
            "test_workflow",
            "test.yml",
            "riscv-image",
            "Run RISC-V candidate image contract",
            None,
            "booley-standard-substrate=docker-image://booley-standard-substrate:ci",
        ),
        (
            "release RISC-V image",
            "release_workflow",
            "docker-publish.yml",
            "build-and-push-riscv",
            None,
            "build",
            "booley-substrate=docker-image://${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}@"
            "${{ steps.substrate.outputs.digest }}",
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
    context = "booley-substrate"
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


@pytest.mark.parametrize("job", ["bwave-smoke", "riscv-image"])
def test_published_base_resolver_must_precede_its_candidate(job: str) -> None:
    sources = _sources()
    steps = sources.test_workflow["jobs"][job]["steps"]
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
            "runtime-base",
            "Resolve compatible stable runtime base",
            "release candidate",
        ),
        pytest.param(
            "release_workflow",
            "docker-publish.yml",
            "runtime-base",
            "Verify candidate stable runtime base",
            "release base selection",
            id="candidate-verify",
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
        "runtime-base",
        "Resolve compatible stable runtime base",
    )
    step["run"] = str(step["run"]).replace("--resolver remote", "--resolver shadow")
    step["run"] += "\n# --resolver remote"

    assert "remote stable-base resolver" in _errors(sources, "release candidate")


@pytest.mark.parametrize(
    ("mutation", "role", "left", "right"),
    [
        ("node-probe", "Node", "agent_policy_probe.py", "booley-test"),
        ("cocotb-authority", "Cocotb", "base-python-requirements.txt", "session-runtime.toml"),
        ("cocotb-build-probe", "Cocotb", "Dockerfile.base", "base-python-requirements.txt"),
        ("cocotb-production-probe", "Cocotb", "base-python-requirements.txt", "test.yml"),
        (
            "cocotb-session-contract",
            "Cocotb",
            "base-python-requirements.txt",
            "session-runtime.toml",
        ),
    ],
    ids=[
        "node-probe",
        "cocotb-authority",
        "cocotb-build-probe",
        "cocotb-production-probe",
        "cocotb-session-contract",
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
    step = _step(
        sources,
        "test_workflow",
        "test.yml",
        "bwave-smoke",
        "Run installed agent CLI policy probe",
    )
    step["run"] = str(step["run"]).replace("booley-test python", "other-image python")
    return sources


def _mutate_cocotb_role(sources: ContractSources, mutation: str) -> ContractSources:
    if mutation == "cocotb-authority":
        return replace(
            sources,
            base_python_requirements=_sub_once(
                r"(?m)^cocotb==.*$", "cocotb==99.0.0", sources.base_python_requirements
            ),
        )
    if mutation == "cocotb-build-probe":
        # A literal version instead of the derivation from the requirements file.
        return replace(
            sources,
            base_dockerfile=_sub_once(
                r'(cocotb-config --version\)"\s*=\s*)"\$\(sed[^"]*\)"',
                r'\g<1>"2.1.0"',
                sources.base_dockerfile,
            ),
        )
    if mutation == "cocotb-session-contract":
        return replace(
            sources,
            session_runtime_contract=_sub_once(
                r'(cocotb-config --version\)" = ")[^"]+',
                r"\g<1>99.0.0",
                sources.session_runtime_contract,
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
        r"(cocotb-config --version\)\\?\"\s*=\s*)\\\"\\\$\(sed[^)]*\)\\\"",
        r"\g<1>2.1.0",
        str(step["run"]),
    )
    return sources


def test_verilator_identity_must_match_the_dockerfile_pin() -> None:
    sources = _sources()
    pinned = replace(
        sources,
        base_dockerfile=_sub_once(
            r"ARG VERILATOR_VERSION=[^\s]+",
            "ARG VERILATOR_VERSION=v9.999",
            sources.base_dockerfile,
        ),
    )

    errors = _errors(pinned, "Verilator")

    assert "Dockerfile.base" in errors
    assert "verilator_identity.py" in errors
    assert "session-runtime.toml" in errors
    live = replace(sources, verilator_identity=(sources.verilator_identity[0], "0" * 40))
    assert "verilator_identity.py" in _errors(live, "Verilator")


def test_session_runtime_verilator_probe_must_match_the_dockerfile_pin() -> None:
    sources = _sources()
    probe = next(
        item
        for item in sources.session_runtime["common"]["probe"]
        if item["name"] == "EDA command diagnostics"
    )
    probe["command"] = probe["command"].replace(
        "Verilator source revision: ", "Verilator source revision: 0"
    )

    errors = _errors(sources, "Verilator")

    assert "Dockerfile.base" in errors
    assert "session-runtime.toml" in errors


def test_node_and_npm_authorities_are_dockerfile_arguments() -> None:
    sources = _sources()
    missing_npm = replace(
        sources,
        base_dockerfile=_sub_once(r"ARG NPM_VERSION=[^\s]+\n", "", sources.base_dockerfile),
    )

    assert "expected one exact version" in _errors(missing_npm, "npm authority")


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
    command = f"python3 -m booley.flows.sim --project /validation-tmp/project --target {target}"
    step["run"] = str(step["run"]).replace(command, "true")

    assert f"missing {target} Simulation" in _errors(sources, "Cocotb")


def test_runtime_authorities_reject_ranges_and_duplicates() -> None:
    sources = _sources()
    ranged = replace(
        sources,
        base_python_requirements=_sub_once(
            r"cocotb==", "cocotb>=", sources.base_python_requirements
        ),
    )
    node_line = re.search(r"^ARG NODE_VERSION=[^\s]+$", sources.base_dockerfile, re.MULTILINE)
    assert node_line is not None
    duplicated = replace(
        sources,
        base_dockerfile=sources.base_dockerfile.replace(
            node_line.group(0), f"{node_line.group(0)}\n{node_line.group(0)}", 1
        ),
    )

    assert "cocotb>=" in _errors(ranged, "exact version")
    assert "expected one exact version" in _errors(duplicated, "Node authority")


# --- #829 runtime-base package inventory --------------------------------------

_INVENTORY_LABEL_LINE = (
    "io.booley.runtime-base.package-inventory=/usr/local/share/booley/base-package-inventory.json"
)


def test_base_must_label_its_package_inventory() -> None:
    sources = replace(
        _sources(),
        base_dockerfile=_sources().base_dockerfile.replace(_INVENTORY_LABEL_LINE, ""),
    )

    assert "must label" in _errors(sources, "package inventory")


@pytest.mark.parametrize(
    "field", ["candidate_dockerfile", "overlay_dockerfile", "riscv_dockerfile"]
)
def test_derived_recipes_must_not_relabel_the_inventory(field: str) -> None:
    sources = _sources()
    sources = replace(
        sources, **{field: getattr(sources, field) + f"\nLABEL {_INVENTORY_LABEL_LINE}\n"}
    )

    assert "must inherit" in _errors(sources, "package inventory")


# Fake Docker: an image's inventory is its own name; other extraction forms fail.
_FAKE_DOCKER = """#!/bin/sh
case "$*" in
  "pull "*) ;;
  "run --rm --network none --entrypoint cat $7 /usr/local/share/booley/base-package-inventory.json")
    echo "$7" ;;
  *) echo "unexpected: docker $*" >&2; exit 64 ;;
esac
"""


@pytest.mark.skipif(sys.platform == "win32", reason="CI inventory steps run under Linux bash")
@pytest.mark.parametrize(
    ("step", "env"),
    [
        pytest.param(
            ("test_workflow", "test.yml", "bwave-smoke", "Export standard package inventory"),
            {"BUILD_STABLE_BASE": "true"},
            id="local-base-standard",
        ),
        pytest.param(None, {}, id="test-standard-riscv"),
        pytest.param(
            (
                "release_workflow",
                "docker-publish.yml",
                "standard-image-contract",
                "Export standard and runtime-base package inventories",
            ),
            {"RUNTIME_BASE": "base", "IMAGE": "standard"},
            id="release-base-standard",
        ),
        pytest.param(
            (
                "release_workflow",
                "docker-publish.yml",
                "riscv-image-contract",
                "Validate exact RISC-V candidate",
            ),
            {"BASE_IMAGE": "standard", "RISCV_IMAGE": "riscv"},
            id="release-standard-riscv",
        ),
    ],
)
def test_parent_inventory_mismatch_fails_the_step(
    tmp_path: Path, step: tuple[str, str, str, str] | None, env: dict[str, str]
) -> None:
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    parent = tmp_path / "docker-build-evidence" / "base-package-inventory.json"
    parent.parent.mkdir()
    parent.write_text("parent\n")
    if step is None:
        script = _ROOT / ".github/scripts/verify_riscv_image_contract.sh"
        argv = [str(script), str(tmp_path), "riscv", "substrate", str(parent)]
    else:
        value = _step(_sources(), *step)
        script = tmp_path / "step.sh"
        script.write_text(value["run"])
        # GitHub's bash shell adds pipefail; a step without `shell:` runs `bash -e`.
        shell = ["--noprofile", "--norc", "-eo", "pipefail"] if value.get("shell") else ["-e"]
        argv = ["bash", *shell, str(script)]
    path = f"{docker.parent}{os.pathsep}{os.environ['PATH']}"
    result = run_bounded_child(
        argv,
        cwd=_ROOT,
        env={**os.environ, "PATH": path, "RUNNER_TEMP": str(tmp_path), **env},
        check=False,
    )

    assert result.returncode != 0
    assert " differ: " in result.stdout, result.stdout + result.stderr


def test_release_riscv_layers_must_prefix_match_the_standard_substrate() -> None:
    """Dockerfile.riscv derives from the substrate; the overlaid image can never match."""
    sources = _sources()
    step = _step(
        sources,
        "release_workflow",
        "docker-publish.yml",
        "riscv-image-contract",
        "Validate exact RISC-V candidate",
    )
    step["run"] = step["run"].replace(
        '--base-image "${STANDARD_SUBSTRATE}"', '--base-image "${BASE_IMAGE}"'
    )

    assert "--base-image" in _errors(sources, "riscv layer contract")


def test_release_riscv_substrate_must_be_the_standard_build_digest() -> None:
    sources = _sources()
    step = _step(
        sources,
        "release_workflow",
        "docker-publish.yml",
        "riscv-image-contract",
        "Validate exact RISC-V candidate",
    )
    step["env"]["STANDARD_SUBSTRATE"] = step["env"]["STANDARD_SUBSTRATE"].replace(
        "build-and-push.outputs", "build-and-push-riscv.outputs"
    )

    assert "STANDARD_SUBSTRATE" in _errors(sources, "riscv layer contract")


def test_release_riscv_substrate_must_be_pulled_before_inspection() -> None:
    sources = _sources()
    step = _step(
        sources,
        "release_workflow",
        "docker-publish.yml",
        "riscv-image-contract",
        "Validate exact RISC-V candidate",
    )
    step["run"] = step["run"].replace('docker pull "${STANDARD_SUBSTRATE}"\n', "")

    assert "docker pull" in _errors(sources, "riscv layer contract")


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ("type=image,name=booley-riscv-substrate:ci,push=true", "must load its image"),
        ("type=image,name=booley-other:ci", "tags"),
        ("type=registry,name=booley-riscv-substrate:ci", "must load its image"),
    ],
    ids=["pushed", "renamed", "registry"],
)
def test_riscv_candidate_must_stay_in_the_local_image_store(output: str, problem: str) -> None:
    """The zstd export is the RISC-V candidate only while it lands under its tag locally."""
    sources = _sources()
    step = _step(
        sources, "test_workflow", "test.yml", "riscv-image", "Run RISC-V candidate image contract"
    )
    step["run"] = _sub_once(
        r"--output \S+",
        f"--output {output}",
        str(step["run"]),
    )

    assert problem in _errors(sources, "local RISC-V candidate")


def _runtime_base_step(sources: ContractSources, step_id: str) -> dict[str, Any]:
    return _step(
        sources, "release_workflow", "docker-publish.yml", "runtime-base", step_id=step_id
    )


def _flip_condition(step: dict[str, Any]) -> None:
    condition = str(step["if"])
    step["if"] = condition.replace("==", "!!").replace("!=", "==").replace("!!", "!=")


def _swap_steps(sources: ContractSources, first: str, second: str) -> None:
    steps = sources.release_workflow["jobs"]["runtime-base"]["steps"]
    ids = [step.get("id") for step in steps]
    steps[ids.index(first)], steps[ids.index(second)] = (
        steps[ids.index(second)],
        steps[ids.index(first)],
    )


# Mutation id -> (candidate-build input, replacement) for single-input edits.
_BASE_BUILD_EDITS: dict[str, tuple[str, object]] = {
    "base-dockerfile": ("file", "src/booley/data/docker/Dockerfile.substrate"),
    "base-push": ("push", False),
    "base-tag": ("tags", "${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}:main"),
}


def _mutate_candidate_wiring(sources: ContractSources, mutation: str) -> bool:
    """Break the candidate tag or the report step's inputs; False for another mutation."""
    if mutation == "metadata-tag":
        step = _runtime_base_step(sources, "candidate-metadata")
        step["run"] = str(step["run"]).replace("tag=candidate-${GITHUB_SHA}", "tag=main")
    elif mutation == "selected-env":
        _runtime_base_step(sources, "selected")["env"]["CANDIDATE_IMAGE"] = (
            "${{ steps.candidate-build.outputs.digest }}"
        )
    else:
        return False
    return True


def _mutate_base_selection(sources: ContractSources, mutation: str) -> None:
    if _mutate_candidate_wiring(sources, mutation):
        return
    jobs = sources.release_workflow["jobs"]
    build = _runtime_base_step(sources, "candidate-build")["with"]
    if mutation == "decide-if":
        _runtime_base_step(sources, "decide")["if"] = "github.event_name == 'push'"
    elif mutation == "published-if":
        _flip_condition(_runtime_base_step(sources, "published"))
    elif mutation.startswith("candidate-if-"):
        _flip_condition(_runtime_base_step(sources, mutation.removeprefix("candidate-if-")))
    elif mutation == "order":
        _swap_steps(sources, "candidate-build", "candidate-verify")
    elif mutation == "needs":
        del jobs["build-and-push"]["needs"]
    elif mutation in _BASE_BUILD_EDITS:
        key, value = _BASE_BUILD_EDITS[mutation]
        build[key] = value
    elif mutation in {"base-cache-from", "base-cache-from-missing"}:
        del build["cache-from"]
        if mutation == "base-cache-from":
            build["cache-from"] = "type=gha,scope=sandbox-substrate"
    elif mutation == "base-args-parity":
        build["build-args"] = str(build["build-args"]).replace("BOOLEY_BASE_BUILT_AT=", "OTHER=")
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param("decide-if", "step decide must run only when", id="decide-if"),
        pytest.param("published-if", "step published must run only when", id="published-if"),
        pytest.param(
            "candidate-if-candidate-metadata",
            "step candidate-metadata must run only when",
            id="candidate-if-metadata",
        ),
        pytest.param(
            "candidate-if-candidate-build",
            "step candidate-build must run only when",
            id="candidate-if-build",
        ),
        pytest.param(
            "candidate-if-candidate-verify",
            "step candidate-verify must run only when",
            id="candidate-if-verify",
        ),
        pytest.param("order", "out of order", id="order"),
        pytest.param("needs", "must depend on the runtime-base job", id="needs"),
        pytest.param("base-dockerfile", "builds", id="base-dockerfile"),
        pytest.param("base-push", "must push its image", id="base-push"),
        pytest.param("base-tag", "disagree with", id="base-tag"),
        pytest.param("base-cache-from", "candidate base must read exactly", id="base-cache-from"),
        pytest.param(
            "base-cache-from-missing",
            "candidate base must read exactly",
            id="base-cache-from-missing",
        ),
        pytest.param("base-args-parity", "must match the publisher", id="base-args-parity"),
        pytest.param("metadata-tag", "candidate tag must be exactly", id="metadata-tag"),
        pytest.param("selected-env", "must read the verified candidate", id="selected-env"),
    ],
)
def test_release_base_selection_rules(mutation: str, expected: str) -> None:
    sources = _sources()
    _mutate_base_selection(sources, mutation)

    assert expected in _errors(sources, "release")


def test_candidate_base_verification_requires_the_build_digest() -> None:
    sources = _sources()
    _runtime_base_step(sources, "candidate-verify")["env"]["BASE_IMAGE"] = (
        "ghcr.io/acme/base:candidate"
    )

    assert "build digest" in _errors(sources, "release base selection")


def test_release_substrate_parent_is_the_selected_base() -> None:
    sources = _sources()
    step = _step(
        sources, "release_workflow", "docker-publish.yml", "build-and-push", step_id="substrate"
    )
    step["with"]["build-contexts"] = (
        "booley-runtime-base=docker-image://${{ steps.runtime-base.outputs.image }}"
    )

    assert "named contexts" in _errors(sources, "release standard substrate")


def _mutate_substrate_tags(sources: ContractSources, mutation: str) -> tuple[str, str]:
    """Break one link of the candidate-scoped tag rule; return the failing role and message."""
    jobs = sources.release_workflow["jobs"]
    if mutation == "standard-tag":
        step = _step(
            sources,
            "release_workflow",
            "docker-publish.yml",
            "build-and-push",
            step_id="substrate",
        )
        step["with"]["tags"] = (
            "${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}:standard-"
            "${{ steps.version.outputs.substrate_inputs }}"
        )
        return "release standard substrate", "tags"
    if mutation == "riscv-tag":
        step = _step(
            sources,
            "release_workflow",
            "docker-publish.yml",
            "build-and-push-riscv",
            step_id="substrate",
        )
        step["with"]["tags"] = (
            "${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}:riscv-"
            "${{ steps.version.outputs.substrate_recipe_fingerprint }}"
        )
        return "release RISC-V substrate", "tags"
    if mutation == "source-output":
        jobs["runtime-base"]["outputs"]["origin"] = jobs["runtime-base"]["outputs"].pop("source")
        return "release base selection", "must expose image, source, candidate-tag"
    jobs["build-and-push"]["outputs"]["base-source"] = "published"
    return "release base selection", "base-source must pass through"


@pytest.mark.parametrize("mutation", ["standard-tag", "riscv-tag", "source-output", "passthrough"])
def test_candidate_base_substrates_take_candidate_scoped_tags(mutation: str) -> None:
    sources = _sources()
    role, expected = _mutate_substrate_tags(sources, mutation)

    assert expected in _errors(sources, role)


@pytest.mark.parametrize("mutation", ["cache-to", "main-tag", "imagetools"])
def test_dispatched_run_never_writes_shared_state(mutation: str) -> None:
    sources = _sources()
    build = _runtime_base_step(sources, "candidate-build")["with"]
    if mutation == "cache-to":
        build["cache-to"] = "type=gha,scope=sandbox-runtime-base,mode=max"
        expected = "must not write a cache scope"
    elif mutation == "main-tag":
        build["tags"] = "${{ env.REGISTRY }}/${{ env.BASE_IMAGE_NAME }}:main"
        expected = "must not tag :main"
    else:
        sources.release_workflow["jobs"]["runtime-base"]["steps"].append(
            {
                "name": "Move main",
                "run": "docker buildx imagetools create --tag ghcr.io/acme/base:main src",
            }
        )
        expected = "must not create tags outside promote"

    assert expected in _errors(sources, "release shared state")


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under Linux bash")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param(
            {"PUBLISHED_IMAGE": "ghcr.io/acme/base@sha256:aaa", "CANDIDATE_IMAGE": ""},
            (
                "image=ghcr.io/acme/base@sha256:aaa",
                "source=published",
                "candidate-tag=",
                "Stable runtime base: published :main ghcr.io/acme/base@sha256:aaa",
            ),
            id="published",
        ),
        pytest.param(
            {
                "PUBLISHED_IMAGE": "",
                "CANDIDATE_IMAGE": "ghcr.io/acme/base@sha256:bbb",
                "CANDIDATE_TAG": "candidate-abc-1-1",
            },
            (
                "image=ghcr.io/acme/base@sha256:bbb",
                "source=candidate",
                "candidate-tag=candidate-abc-1-1",
                "Stable runtime base: candidate built from source, tag candidate-abc-1-1, "
                "ghcr.io/acme/base@sha256:bbb",
            ),
            id="candidate",
        ),
    ],
)
def test_selected_base_report(
    tmp_path: Path, env: dict[str, str], expected: tuple[str, ...]
) -> None:
    """The report step names the base it selected, with its digest, in outputs and summary."""
    step = _runtime_base_step(_sources(), "selected")
    script = tmp_path / "step.sh"
    script.write_text(step["run"], newline="\n")
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    result = run_bounded_child(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "CANDIDATE_TAG": "",
            **env,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == list(expected[:3])
    assert result.stdout.strip() == expected[3]
    assert summary.read_text(encoding="utf-8").strip() == expected[3]
