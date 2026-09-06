"""Parsed static contract for Booley's CI-owned Session Image graph."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_BASE_DOCKERFILE = "src/booley/data/docker/Dockerfile.base"
_CANDIDATE_DOCKERFILE = "src/booley/data/docker/Dockerfile"
_RISCV_DOCKERFILE = "src/booley/data/docker/Dockerfile.riscv"
_BUILD_ACTION = "docker/build-push-action@"
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")


@dataclass(frozen=True)
class Instruction:
    keyword: str
    value: str
    line: int


@dataclass(frozen=True)
class WorkflowStep:
    workflow: str
    job: str
    path: tuple[int, ...]
    value: dict[str, Any]

    @property
    def locator(self) -> str:
        label = self.value.get("name") or self.value.get("id") or self.path
        return f"{self.workflow}:{self.job}:{label}"


@dataclass(frozen=True)
class Build:
    step: WorkflowStep
    dockerfile: str
    target: str
    contexts: dict[str, str]
    build_args: dict[str, str]
    labels: dict[str, str]
    tags: tuple[str, ...]
    load: bool
    push: bool


@dataclass(frozen=True)
class ContractSources:
    base_dockerfile: str
    candidate_dockerfile: str
    riscv_dockerfile: str
    test_workflow: dict[str, Any]
    release_workflow: dict[str, Any]
    base_workflow: dict[str, Any]


@dataclass(frozen=True)
class RuntimeRole:
    name: str
    authority: str
    probes: tuple[str, ...]
    constraint: str = "exact"


_RUNTIME_ROLES = {
    "node": RuntimeRole(
        "Node",
        "Dockerfile.base:ARG NODE_VERSION",
        ("test.yml:bwave-smoke:agent_policy_probe.py --expected-node",),
    ),
    "cocotb": RuntimeRole(
        "Cocotb",
        "Dockerfile.base:pip requirement",
        (
            "Dockerfile.base:cocotb-config build assertion",
            "test.yml:bwave-smoke:cocotb-config production probe",
        ),
    ),
}


def load_sources(repo: Path) -> ContractSources:
    docker_dir = repo / "src/booley/data/docker"
    return ContractSources(
        base_dockerfile=(docker_dir / "Dockerfile.base").read_text(encoding="utf-8"),
        candidate_dockerfile=(docker_dir / "Dockerfile").read_text(encoding="utf-8"),
        riscv_dockerfile=(docker_dir / "Dockerfile.riscv").read_text(encoding="utf-8"),
        test_workflow=_load_yaml(repo / ".github/workflows/test.yml"),
        release_workflow=_load_yaml(repo / ".github/workflows/docker-publish.yml"),
        base_workflow=_load_yaml(repo / ".github/workflows/docker-base-publish.yml"),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: workflow must be a mapping")
    return value


def logical_instructions(contents: str) -> tuple[Instruction, ...]:
    instructions: list[Instruction] = []
    parts: list[str] = []
    start = 0
    for number, raw in enumerate(contents.splitlines(), 1):
        stripped = raw.strip()
        if not parts and (not stripped or stripped.startswith("#")):
            continue
        if not parts:
            start = number
        continued = raw.rstrip().endswith("\\")
        part = raw.rstrip()[:-1] if continued else raw
        parts.append(part.strip())
        if continued:
            continue
        logical = " ".join(parts)
        keyword, separator, value = logical.partition(" ")
        if separator:
            instructions.append(Instruction(keyword.upper(), value.strip(), start))
        parts = []
    if parts:
        raise ValueError(f"Dockerfile:{start}: unterminated logical instruction")
    return tuple(instructions)


def dockerfile_parents(contents: str) -> tuple[str, ...]:
    arguments: dict[str, str] = {}
    parents: list[str] = []
    for instruction in logical_instructions(contents):
        if instruction.keyword == "ARG":
            name, separator, value = instruction.value.partition("=")
            if separator:
                arguments[name] = value
        elif instruction.keyword == "FROM":
            fields = shlex.split(instruction.value)
            image = next((field for field in fields if not field.startswith("--")), "")
            resolved = _expand_arguments(image, arguments)
            if not resolved or "$" in resolved:
                raise ValueError(f"Dockerfile:{instruction.line}: unresolved FROM {image}")
            parents.append(resolved)
    return tuple(parents)


def _expand_arguments(value: str, arguments: dict[str, str]) -> str:
    pattern = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

    def replace(match: re.Match[str]) -> str:
        return arguments.get(match.group(1) or match.group(2), match.group(0))

    return pattern.sub(replace, value)


def _flatten_steps(
    workflow_name: str,
    job_name: str,
    values: object,
    prefix: tuple[int, ...] = (),
) -> tuple[WorkflowStep, ...]:
    if not isinstance(values, list):
        return ()
    flattened: list[WorkflowStep] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        path = (*prefix, index)
        if "parallel" in value:
            flattened.extend(_flatten_steps(workflow_name, job_name, value["parallel"], path))
        else:
            flattened.append(WorkflowStep(workflow_name, job_name, path, value))
    return tuple(flattened)


def workflow_steps(
    workflow_name: str, workflow: dict[str, Any], job_name: str
) -> tuple[WorkflowStep, ...]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get(job_name), dict):
        raise ValueError(f"{workflow_name}:{job_name}: missing job")
    return _flatten_steps(workflow_name, job_name, jobs[job_name].get("steps"))


def find_step(
    workflow_name: str,
    workflow: dict[str, Any],
    job_name: str,
    *,
    name: str | None = None,
    step_id: str | None = None,
) -> WorkflowStep:
    matches = [
        step
        for step in workflow_steps(workflow_name, workflow, job_name)
        if (name is None or step.value.get("name") == name)
        and (step_id is None or step.value.get("id") == step_id)
    ]
    if len(matches) != 1:
        label = name or step_id
        raise ValueError(
            f"{workflow_name}:{job_name}: expected one step {label}, found {len(matches)}"
        )
    return matches[0]


def parse_build(step: WorkflowStep) -> Build:
    uses = str(step.value.get("uses", ""))
    if uses.startswith(_BUILD_ACTION):
        return _action_build(step)
    run = str(step.value.get("run", ""))
    if "docker buildx build" in run:
        return _shell_build(step, run)
    raise ValueError(f"{step.locator}: not a supported BuildKit step")


def _action_build(step: WorkflowStep) -> Build:
    inputs = step.value.get("with")
    if not isinstance(inputs, dict):
        raise ValueError(f"{step.locator}: build inputs must be a mapping")
    return Build(
        step=step,
        dockerfile=str(inputs.get("file", "")),
        target=str(inputs.get("target", "")),
        contexts=_key_values(inputs.get("build-contexts")),
        build_args=_key_values(inputs.get("build-args")),
        labels=_key_values(inputs.get("labels")),
        tags=_lines(inputs.get("tags")),
        load=inputs.get("load") is True,
        push=inputs.get("push") is True,
    )


def _shell_build(step: WorkflowStep, run: str) -> Build:
    command = next(
        line for line in run.replace("\\\n", " ").splitlines() if "docker buildx build" in line
    )
    fields = shlex.split(command.partition("docker buildx build")[2])
    fields = fields[: fields.index("|")] if "|" in fields else fields
    return Build(
        step=step,
        dockerfile=_option(fields, "--file"),
        target=_option(fields, "--target"),
        contexts=_repeated_key_values(fields, "--build-context"),
        build_args=_repeated_key_values(fields, "--build-arg"),
        labels={},
        tags=tuple(_options(fields, "--tag")),
        load="--load" in fields,
        push="--push" in fields,
    )


def _lines(value: object) -> tuple[str, ...]:
    return tuple(line.strip() for line in str(value or "").splitlines() if line.strip())


def _key_values(value: object) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in _lines(value):
        key, separator, item = line.partition("=")
        if separator:
            pairs[key] = item
    return pairs


def _options(fields: list[str], option: str) -> list[str]:
    return [fields[index + 1] for index, field in enumerate(fields[:-1]) if field == option]


def _option(fields: list[str], option: str) -> str:
    values = _options(fields, option)
    return values[0] if len(values) == 1 else ""


def _repeated_key_values(fields: list[str], option: str) -> dict[str, str]:
    return _key_values("\n".join(_options(fields, option)))


def validate_sources(sources: ContractSources) -> tuple[str, ...]:
    errors: list[str] = []
    errors.extend(_dockerfile_errors(sources))
    errors.extend(_stable_base_errors(sources.base_workflow))
    errors.extend(_test_graph_errors(sources.test_workflow))
    errors.extend(_release_graph_errors(sources.release_workflow))
    errors.extend(_runtime_role_errors(sources))
    return tuple(errors)


def _dockerfile_errors(sources: ContractSources) -> list[str]:
    errors: list[str] = []
    for role, contents, expected in (
        ("candidate", sources.candidate_dockerfile, "booley-runtime-base"),
        ("riscv", sources.riscv_dockerfile, "booley-sandbox"),
    ):
        try:
            parents = dockerfile_parents(contents)
        except ValueError as error:
            errors.append(f"{role} image: {error}")
            continue
        if expected not in parents:
            errors.append(
                f"{role} image: Dockerfile parent {expected!r} is missing; found {parents}"
            )
    return errors


def _stable_base_errors(workflow: dict[str, Any]) -> list[str]:
    role = "stable-base publish"
    try:
        build_step = find_step(
            "docker-base-publish.yml", workflow, "build-publish-smoke", step_id="build"
        )
        verify = find_step(
            "docker-base-publish.yml",
            workflow,
            "build-publish-smoke",
            name="Verify exact published base compatibility and tools",
        )
        promote = find_step(
            "docker-base-publish.yml",
            workflow,
            "build-publish-smoke",
            name="Promote verified base to main consumption tag",
        )
        build = parse_build(build_step)
    except ValueError as error:
        return [f"{role}: {error}"]
    errors = _build_shape_errors(role, build, _BASE_DOCKERFILE, {}, (), push=True)
    expected = "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${{ steps.build.outputs.digest }}"
    if not (build_step.path < verify.path < promote.path):
        errors.append(f"{role}: build, exact verification, and promotion are out of order")
    for step_role, step in (("verification", verify), ("promotion", promote)):
        environment = step.value.get("env", {})
        if not isinstance(environment, dict) or environment.get("BASE_IMAGE") != expected:
            errors.append(f"{role}: {step_role} must consume stable-base build digest {expected}")
    errors.extend(_resolver_errors(role, verify, "${BASE_IMAGE}", github_output=False))
    return errors


def _test_graph_errors(workflow: dict[str, Any]) -> list[str]:
    job = "bwave-smoke"
    name = "test.yml"
    try:
        resolver = find_step(name, workflow, job, name="Select compatible stable runtime base")
        base_step = find_step(
            name, workflow, job, name="Build changed stable runtime base locally"
        )
        local_step = find_step(
            name, workflow, job, name="Build candidate from changed stable base"
        )
        remote_step = find_step(
            name, workflow, job, name="Build candidate from published stable base"
        )
        riscv_step = find_step(name, workflow, job, name="Run RISC-V candidate image contract")
        base, local, remote, riscv = map(
            parse_build, (base_step, local_step, remote_step, riscv_step)
        )
    except ValueError as error:
        return [f"test image graph: {error}"]
    errors = _resolver_errors("published-base test candidate", resolver, "${PUBLISHED_BASE}")
    if resolver.path >= remote.step.path:
        errors.append("published-base test candidate: resolver must precede its candidate build")
    errors.extend(_test_alternative_errors(base, local, remote))
    errors.extend(_test_build_shape_errors(local, remote, riscv))
    return errors


def _test_build_shape_errors(local: Build, remote: Build, riscv: Build) -> list[str]:
    errors: list[str] = []
    errors.extend(
        _build_shape_errors(
            "local-base candidate",
            local,
            _CANDIDATE_DOCKERFILE,
            {"booley-runtime-base": "docker-image://booley-runtime-base:ci"},
            ("booley-test",),
            load=True,
            build_arg=("BOOLEY_RUNTIME_BASE_IMAGE", "booley-runtime-base:ci"),
        )
    )
    remote_parent = "docker-image://${{ steps.runtime-base.outputs.image }}"
    errors.extend(
        _build_shape_errors(
            "published-base test candidate",
            remote,
            _CANDIDATE_DOCKERFILE,
            {"booley-runtime-base": remote_parent},
            ("booley-test",),
            load=True,
            build_arg=("BOOLEY_RUNTIME_BASE_IMAGE", "${{ steps.runtime-base.outputs.image }}"),
        )
    )
    errors.extend(
        _build_shape_errors(
            "local RISC-V candidate",
            riscv,
            _RISCV_DOCKERFILE,
            {"booley-sandbox": "docker-image://booley-test"},
            ("booley-riscv-test",),
            load=True,
        )
    )
    return errors


def _test_alternative_errors(base: Build, local: Build, remote: Build) -> list[str]:
    role = "test candidate alternatives"
    errors = _build_shape_errors(
        "local stable-base producer",
        base,
        _BASE_DOCKERFILE,
        {},
        ("booley-runtime-base:ci",),
        load=True,
    )
    local_condition = _condition(base.step.value.get("if"))
    candidate_condition = _condition(local.step.value.get("if"))
    remote_condition = _condition(remote.step.value.get("if"))
    local_expected = ("steps.runtime-base.outputs.build", "==", "true")
    remote_expected = ("steps.runtime-base.outputs.build", "!=", "true")
    if local_condition != local_expected or candidate_condition != local_expected:
        errors.append(f"{role}: local builds must use the runtime-base build selector")
    if remote_condition != remote_expected:
        errors.append(f"{role}: published build must complement the runtime-base build selector")
    if base.step.path >= local.step.path:
        errors.append(f"{role}: local stable-base producer must precede its candidate")
    return errors


def _condition(value: object) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)\s*(==|!=)\s*'([^']+)'\s*", str(value or ""))
    return match.groups() if match else None


def _release_graph_errors(workflow: dict[str, Any]) -> list[str]:
    try:
        resolver = find_step(
            "docker-publish.yml",
            workflow,
            "build-and-push",
            name="Resolve compatible stable runtime base",
        )
        candidate = parse_build(
            find_step("docker-publish.yml", workflow, "build-and-push", step_id="build")
        )
        riscv = parse_build(
            find_step("docker-publish.yml", workflow, "build-and-push-riscv", step_id="build")
        )
    except ValueError as error:
        return [f"release image graph: {error}"]
    errors = _resolver_errors("release candidate", resolver, "${BASE_REF}")
    errors.extend(_release_candidate_errors(workflow, candidate))
    errors.extend(_release_riscv_errors(workflow, riscv))
    return errors


def _release_candidate_errors(workflow: dict[str, Any], build: Build) -> list[str]:
    role = "release candidate"
    parent = "${{ steps.runtime-base.outputs.image }}"
    errors = _build_shape_errors(
        role,
        build,
        _CANDIDATE_DOCKERFILE,
        {"booley-runtime-base": f"docker-image://{parent}"},
        (),
        push=True,
        build_arg=("BOOLEY_RUNTIME_BASE_IMAGE", parent),
        label=("io.booley.build.parent-artifact", parent),
    )
    if build.labels.get("io.booley.build.parent-artifact-kind") != "registry-digest":
        errors.append(f"{role}: parent artifact kind must be registry-digest")
    job = workflow["jobs"]["build-and-push"]
    if job.get("outputs", {}).get("image-digest") != "${{ steps.build.outputs.digest }}":
        errors.append(f"{role}: image-digest must expose the build step's exact digest")
    if (
        build.step.path
        <= find_step(
            "docker-publish.yml",
            workflow,
            "build-and-push",
            name="Resolve compatible stable runtime base",
        ).path
    ):
        errors.append(f"{role}: resolved published base must precede the candidate build")
    return errors


def _release_riscv_errors(workflow: dict[str, Any], build: Build) -> list[str]:
    role = "release RISC-V image"
    parent = (
        "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@"
        "${{ needs.build-and-push.outputs.image-digest }}"
    )
    errors = _build_shape_errors(
        role,
        build,
        _RISCV_DOCKERFILE,
        {"booley-sandbox": f"docker-image://{parent}"},
        (),
        push=True,
        label=("io.booley.build.parent-artifact", parent),
    )
    if build.labels.get("io.booley.build.parent-artifact-kind") != "registry-digest":
        errors.append(f"{role}: parent artifact kind must be registry-digest")
    job = workflow["jobs"]["build-and-push-riscv"]
    needs = job.get("needs")
    if needs not in ("build-and-push", ["build-and-push"]):
        errors.append(f"{role}: job must depend on build-and-push")
    if job.get("outputs", {}).get("image-digest") != "${{ steps.build.outputs.digest }}":
        errors.append(f"{role}: image-digest must expose the build step's exact digest")
    return errors


def _build_shape_errors(
    role: str,
    build: Build,
    dockerfile: str,
    contexts: dict[str, str],
    tags: tuple[str, ...],
    *,
    load: bool = False,
    push: bool = False,
    build_arg: tuple[str, str] | None = None,
    label: tuple[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if build.dockerfile != dockerfile:
        errors.append(
            f"{role}: {build.step.locator} builds {build.dockerfile!r}, expected {dockerfile}"
        )
    if build.target:
        errors.append(
            f"{role}: {build.step.locator} targets intermediate stage {build.target!r}; "
            "expected the final image"
        )
    if build.contexts != contexts:
        errors.append(
            f"{role}: {build.step.locator} named contexts {build.contexts} disagree with {contexts}"
        )
    if tags and build.tags != tags:
        errors.append(f"{role}: {build.step.locator} tags {build.tags} disagree with {tags}")
    if load and not build.load:
        errors.append(f"{role}: {build.step.locator} must load its image")
    if push and not build.push:
        errors.append(f"{role}: {build.step.locator} must push its image")
    if build_arg and build.build_args.get(build_arg[0]) != build_arg[1]:
        errors.append(f"{role}: {build_arg[0]} disagrees with its parent context")
    if label and build.labels.get(label[0]) != label[1]:
        errors.append(f"{role}: {label[0]} disagrees with its exact parent")
    return errors


def _resolver_errors(
    role: str,
    step: WorkflowStep,
    image: str,
    github_output: bool = True,
) -> list[str]:
    try:
        fields = _shell_command(
            str(step.value.get("run", "")), ".github/scripts/docker_base_contract.py"
        )
    except ValueError as error:
        return [f"{role}: {step.locator}: {error}"]
    errors: list[str] = []
    if _option(fields, "--resolver") != "remote":
        errors.append(f"{role}: {step.locator} must use the remote stable-base resolver")
    if _option(fields, "--resolve-image") != image:
        errors.append(f"{role}: {step.locator} resolves the wrong base instead of {image}")
    output = _option(fields, "--github-output")
    if github_output != (output == "${GITHUB_OUTPUT}"):
        expectation = "write" if github_output else "not write"
        errors.append(f"{role}: {step.locator} must {expectation} a resolver output")
    return errors


def _shell_command(run: str, executable: str) -> list[str]:
    commands = []
    for line in run.replace("\\\n", " ").splitlines():
        fields = _shell_fields(line, comments=True)
        if executable in fields:
            commands.append(fields)
    if len(commands) != 1:
        raise ValueError(f"expected one {executable} command, found {len(commands)}")
    return commands[0]


def _runtime_role_errors(sources: ContractSources) -> list[str]:
    errors: list[str] = []
    for validator in (_node_role_errors, _cocotb_role_errors):
        try:
            errors.extend(validator(sources))
        except ValueError as error:
            errors.append(str(error))
    return errors


def _node_role_errors(sources: ContractSources) -> list[str]:
    role = _RUNTIME_ROLES["node"]
    authority = _unique_version(
        f"{role.name} authority {role.authority}",
        _argument_values(sources.base_dockerfile, "NODE_VERSION"),
    )
    probe = find_step(
        "test.yml",
        sources.test_workflow,
        "bwave-smoke",
        name="Run installed agent CLI policy probe",
    )
    evidence = _node_probe_version(str(probe.value.get("run", "")))
    return _version_pair_errors(role, authority, evidence, role.probes[0])


def _cocotb_role_errors(sources: ContractSources) -> list[str]:
    role = _RUNTIME_ROLES["cocotb"]
    authority = _unique_version(
        f"{role.name} authority {role.authority}",
        _cocotb_requirements(sources.base_dockerfile),
    )
    build_evidence = _unique_version(
        "Cocotb build probe Dockerfile.base:cocotb-config",
        _cocotb_assertions(sources.base_dockerfile),
    )
    smoke = find_step(
        "test.yml",
        sources.test_workflow,
        "bwave-smoke",
        name="Run cocotb Icarus/Verilator production-image flows",
    )
    image, command = _docker_run(smoke)
    errors = _version_pair_errors(role, authority, build_evidence, role.probes[0])
    if image != "booley-test":
        errors.append(f"Cocotb exact role: production probe image is {image!r}, not 'booley-test'")
    smoke_evidence, flow_errors = _cocotb_smoke_evidence(command)
    errors.extend(flow_errors)
    errors.extend(_version_pair_errors(role, authority, smoke_evidence, role.probes[1]))
    return errors


def _docker_run(step: WorkflowStep) -> tuple[str, tuple[str, ...]]:
    fields = _shell_fields(str(step.value.get("run", "")).replace("\\\n", " "))
    indexes = [index for index, field in enumerate(fields) if field == "docker"]
    if len(indexes) != 1 or fields[indexes[0] : indexes[0] + 2] != ["docker", "run"]:
        raise ValueError(f"{step.locator}: expected one docker run command")
    index = indexes[0] + 2
    valueless = {"--rm", "--init"}
    while index < len(fields) and fields[index].startswith("-"):
        index += 1 if fields[index] in valueless or "=" in fields[index] else 2
    if index >= len(fields):
        raise ValueError(f"{step.locator}: docker run image is missing")
    return fields[index], tuple(fields[index + 1 :])


def _cocotb_smoke_evidence(command: tuple[str, ...]) -> tuple[str, list[str]]:
    if len(command) != 3 or command[:2] != ("bash", "-c"):
        raise ValueError("Cocotb production probe test.yml: expected a bash -c command")
    segments = _shell_segments(command[2])
    versions = tuple(
        segment[3]
        for segment in segments
        if len(segment) == 4 and segment[:3] == ("test", "$(cocotb-config --version)", "=")
    )
    errors: list[str] = []
    for target in ("sim_icarus", "sim_verilator"):
        expected = (
            "python3",
            "-m",
            "booley.flows.sim",
            "--work-dir",
            "/validation-tmp/project",
            "--target",
            target,
        )
        if expected not in segments:
            errors.append(f"Cocotb exact role: production probe is missing {target} Simulation")
    return _unique_version("Cocotb production probe test.yml:cocotb-config", versions), errors


def _shell_segments(command: str) -> tuple[tuple[str, ...], ...]:
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for field in _shell_fields(command.replace("\\$", "$")):
        if field == "&&":
            segments.append(tuple(current))
            current = []
        else:
            current.append(field)
    segments.append(tuple(current))
    return tuple(segments)


def _shell_fields(command: str, *, comments: bool = False) -> list[str]:
    expressions: list[str] = []

    def mask(match: re.Match[str]) -> str:
        expressions.append(match.group(0))
        return f"__BOOLEY_EXPRESSION_{len(expressions) - 1}__"

    masked = re.sub(r"\$\{\{.*?\}\}", mask, command)
    fields = shlex.split(masked, comments=comments)
    for index, expression in enumerate(expressions):
        placeholder = f"__BOOLEY_EXPRESSION_{index}__"
        fields = [field.replace(placeholder, expression) for field in fields]
    return fields


def _argument_values(contents: str, name: str) -> tuple[str, ...]:
    prefix = f"{name}="
    return tuple(
        instruction.value[len(prefix) :]
        for instruction in logical_instructions(contents)
        if instruction.keyword == "ARG" and instruction.value.startswith(prefix)
    )


def _cocotb_requirements(contents: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for instruction in logical_instructions(contents)
        if instruction.keyword == "RUN"
        for match in re.finditer(r"(?:^|[\s\"'])cocotb==([^\s\"']+)", instruction.value)
    )


def _cocotb_assertions(contents: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for instruction in logical_instructions(contents)
        if instruction.keyword == "RUN"
        for match in re.finditer(r'cocotb-config --version\)"\s*=\s*"([^\"]+)"', instruction.value)
    )


def _unique_version(locator: str, values: tuple[str, ...]) -> str:
    if len(values) != 1 or _VERSION.fullmatch(values[0]) is None:
        raise ValueError(f"{locator}: expected one exact version, found {values}")
    return values[0]


def _node_probe_version(run: str) -> str:
    match = re.search(
        r"booley-test\s+python\s+/work/tests/docker/agent_policy_probe\.py\s+"
        r".*?--expected-node\s+([^\s]+)",
        run.replace("\n", " "),
    )
    if match is None:
        raise ValueError("Node probe test.yml: expected agent_policy_probe.py against booley-test")
    return match.group(1)


def _version_pair_errors(
    role: RuntimeRole, authority: str, evidence: str, locator: str
) -> list[str]:
    if _VERSION.fullmatch(evidence) is None or evidence != authority:
        return [
            f"{role.name} {role.constraint} role: {role.authority}={authority!r} "
            f"disagrees with {locator}={evidence!r}"
        ]
    return []
