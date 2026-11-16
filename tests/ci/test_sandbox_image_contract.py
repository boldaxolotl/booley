"""Supply-chain properties of the workflows that publish Sandbox Images.

Each test states one property over every pushing build or promotion step, never
a step name, job layout or text. Dockerfile properties live in
``tests/docker/test_sandbox_dockerfile.py``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
_WORKFLOWS = _ROOT / ".github/workflows"
# The only workflows that push an image or run `imagetools create`.
_PUBLISH_WORKFLOWS = ("docker-publish.yml", "docker-base-publish.yml", "riscv-tooling-publish.yml")
# The command that checks the built image before its tag is promoted.
_VERIFIERS = {
    "docker-base-publish.yml": "docker_base_contract.py",
    "riscv-tooling-publish.yml": "riscv_tooling.py checks",
}
_OUTPUT = r"\$\{\{ (?:steps|needs)\.([\w-]+)\.outputs\.([\w-]+) \}\}"


def _workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def _steps(workflow: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for job, body in workflow["jobs"].items():
        for step in body.get("steps") or []:
            yield job, step


def _pushes_image(step: dict[str, Any]) -> bool:
    inputs = step.get("with") or {}
    if not str(step.get("uses", "")).startswith("docker/build-push-action@"):
        return False
    return inputs.get("push") is True or "push=true" in str(inputs.get("outputs", ""))


def _promotes(step: dict[str, Any]) -> bool:
    return "imagetools create" in str(step.get("run", ""))


def _pushing_builds() -> Iterator[tuple[str, dict[str, Any], str, dict[str, Any]]]:
    """Yield (workflow name, workflow, job, step) for every build that pushes an image."""
    for name in _PUBLISH_WORKFLOWS:
        workflow = _workflow(name)
        for job, step in _steps(workflow):
            if _pushes_image(step):
                yield name, workflow, job, step


def _lines(value: object) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _step_by_id(workflow: dict[str, Any], job: str, step_id: str) -> dict[str, Any]:
    steps = workflow["jobs"][job].get("steps") or []
    return next((step for step in steps if step.get("id") == step_id), {})


def _named_step(workflow_name: str, job: str, name: str) -> dict[str, Any]:
    steps = _workflow(workflow_name)["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)


def _is_resolver_image(workflow: dict[str, Any], job: str, value: object) -> bool:
    """True when ``value`` is the ``image`` output of a digest-resolving step."""
    match = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outputs\.image \}\}", str(value))
    if match is None:
        return False
    run = str(_step_by_id(workflow, job, match[1]).get("run", ""))
    return "docker_base_contract.py" in run and "--resolve-image" in run


def _traces_to_resolver(workflow: dict[str, Any], reference: str) -> bool:
    """Follow ``needs.<job>.outputs.<name>`` to a step whose images the resolver returned."""
    needed = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.outputs\.([\w-]+) \}\}", reference)
    if needed is None:
        return False
    job, output = needed.groups()
    produced = re.fullmatch(_OUTPUT, str(workflow["jobs"][job].get("outputs", {}).get(output)))
    if produced is None:
        return False
    env = _step_by_id(workflow, job, produced[1]).get("env") or {}
    images = [value for key, value in env.items() if key.endswith("_IMAGE")]
    return bool(images) and all(_is_resolver_image(workflow, job, value) for value in images)


def test_release_build_parents_are_pinned_by_digest() -> None:
    """A published build takes each parent image by digest, never by a movable tag."""
    seen = 0
    for name, workflow, _job, step in _pushing_builds():
        for line in _lines(step["with"].get("build-contexts")):
            seen += 1
            reference = line.partition("=docker-image://")[2]
            pinned = re.search(r"@\$\{\{ [\w.-]+digest \}\}$", reference)
            assert pinned or _traces_to_resolver(workflow, reference), f"{name}: {line}"
    assert seen


def test_published_builds_push_no_shared_tag() -> None:
    """Shared tags move only through promotion, never from a build."""
    for name, _wf, _job, step in _pushing_builds():
        for tag in _lines(step["with"].get("tags")):
            assert not re.search(r":(latest|main)$", tag), f"{name}: {tag}"


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under Linux bash")
def test_computed_candidate_tags_are_run_scoped(tmp_path: Path) -> None:
    """A tag computed by an earlier step names this run, so it cannot be a shared tag."""
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', newline="\n")
    (shim / "python").chmod(0o755)
    fake = {"GITHUB_SHA": "sha-111", "GITHUB_RUN_ID": "run-222", "GITHUB_RUN_ATTEMPT": "try-3"}
    ran = 0
    for name, workflow, job, step in _pushing_builds():
        for line in _lines(step["with"].get("tags")):
            computed = re.search(r":\$\{\{ steps\.([\w-]+)\.outputs\.([\w-]+) \}\}$", line)
            if computed is None:
                continue
            script = tmp_path / f"producer-{ran}.sh"
            script.write_text(
                str(_step_by_id(workflow, job, computed[1])["run"]), encoding="utf-8", newline="\n"
            )
            output = tmp_path / f"output-{ran}"
            env = {
                **os.environ,
                **fake,
                "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(output),
            }
            result = run_bounded_child(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                cwd=_ROOT,
                env=env,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            values = dict(
                entry.partition("=")[::2]
                for entry in output.read_text(encoding="utf-8").splitlines()
            )
            for token in fake.values():
                assert token in values[computed[2]], f"{name}: {line} lacks {token}"
            ran += 1
    assert ran


def _digest_flow(steps: list[dict[str, Any]], build: int, end: int) -> tuple[list[int], list[str]]:
    """Return the steps between ``build`` and ``end`` that consume the build's digest.

    A step consumes it directly or through the output of an earlier consumer.
    The second value lists every reference that counts as the digest by ``end``.
    """
    references = [f"steps.{steps[build].get('id')}.outputs.digest"]
    consumers: list[int] = []
    for index in range(build + 1, end):
        if any(reference in json.dumps(steps[index]) for reference in references):
            consumers.append(index)
            if steps[index].get("id"):
                references.append(f"steps.{steps[index]['id']}.outputs.")
    return consumers, references


def test_promotion_follows_verification_of_the_built_digest() -> None:
    """A job that builds and then promotes verifies the built digest in between."""
    checked = 0
    for name, verifier in _VERIFIERS.items():
        workflow = _workflow(name)
        for job, body in workflow["jobs"].items():
            steps: list[dict[str, Any]] = body.get("steps") or []
            builds = [i for i, step in enumerate(steps) if _pushes_image(step)]
            promotions = [i for i, step in enumerate(steps) if _promotes(step)]
            if not builds or not promotions:
                continue
            checked += 1
            promotion = steps[promotions[0]]
            consumers, references = _digest_flow(steps, builds[0], promotions[0])
            verified = [
                index
                for index in consumers
                if verifier in str(steps[index].get("run", ""))
                and steps[index].get("if") == promotion.get("if")
            ]
            where = f"{name}:{job}"
            assert verified, f"{where}: no {verifier} step on the built digest before promotion"
            assert any(reference in json.dumps(promotion) for reference in references), where
    assert checked == len(_VERIFIERS)


def _needed(workflow: dict[str, Any], job: str) -> set[str]:
    """Return every job that ``job`` transitively needs."""
    found: set[str] = set()
    pending = [job]
    while pending:
        needs = workflow["jobs"][pending.pop()].get("needs") or []
        for needed in [needs] if isinstance(needs, str) else needs:
            if needed not in found:
                found.add(needed)
                pending.append(needed)
    return found


def test_release_promotion_needs_every_other_job() -> None:
    """A job that promotes without building waits for every other job and uses digests."""
    promoting = 0
    for name in _PUBLISH_WORKFLOWS:
        workflow = _workflow(name)
        for job, body in workflow["jobs"].items():
            steps: list[dict[str, Any]] = body.get("steps") or []
            if any(_pushes_image(step) for step in steps):
                continue
            for step in filter(_promotes, steps):
                promoting += 1
                assert set(workflow["jobs"]) - {job} <= _needed(workflow, job), f"{name}:{job}"
                env = step.get("env") or {}
                for source in str(step["run"]).split("imagetools create")[1:]:
                    names = re.findall(r"@\$\{(\w+)\}", source)
                    assert names, f"{name}:{job} promotes a source that is not a digest"
                    assert all("digest" in str(env.get(item, "")) for item in names)
    assert promoting


def test_published_builds_carry_an_sbom() -> None:
    for name, _wf, job, step in _pushing_builds():
        assert step["with"].get("sbom") is True, f"{name}:{job}"


def _pull_request_workflows() -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = _workflow(path.name)
        triggers = workflow.get("on", workflow.get(True)) or {}
        if {"pull_request", "pull_request_target"} & set(triggers):
            yield path.name, workflow


def test_pull_request_workflows_hold_no_package_write_token() -> None:
    """Code from a pull request never holds a token that can write a registry package."""
    seen = 0
    for name, workflow in _pull_request_workflows():
        seen += 1
        grants = [workflow.get("permissions")]
        grants += [job.get("permissions") for job in workflow["jobs"].values()]
        for grant in grants:
            assert grant != "write-all", name
            assert not (isinstance(grant, dict) and grant.get("packages") == "write"), name
        for _job, step in _steps(workflow):
            assert not _pushes_image(step) and not _promotes(step), name
    assert seen


def test_shared_docker_cache_is_written_only_from_main() -> None:
    """Pull-request runs and dispatched release runs never write a shared cache scope."""
    writers = 0
    for name, workflow in _pull_request_workflows():
        for _job, step in _steps(workflow):
            if "cache-to" in (step.get("with") or {}):
                writers += 1
                expression = str(step["with"]["cache-to"])
                assert "github.ref == 'refs/heads/main'" in expression, name
    assert writers
    for _job, step in _steps(_workflow("docker-publish.yml")):
        assert "cache-to" not in (step.get("with") or {})


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
            ("test.yml", "bwave-smoke", "Export standard package inventory"),
            {"BUILD_STABLE_BASE": "true"},
            id="local-base-standard",
        ),
        pytest.param(None, {}, id="test-standard-riscv"),
        pytest.param(
            (
                "docker-publish.yml",
                "standard-image-contract",
                "Export standard and runtime-base package inventories",
            ),
            {"RUNTIME_BASE": "base", "IMAGE": "standard"},
            id="release-base-standard",
        ),
        pytest.param(
            ("docker-publish.yml", "riscv-image-contract", "Validate exact RISC-V candidate"),
            {"BASE_IMAGE": "standard", "RISCV_IMAGE": "riscv"},
            id="release-standard-riscv",
        ),
    ],
)
def test_parent_inventory_mismatch_fails_the_step(
    tmp_path: Path, step: tuple[str, str, str] | None, env: dict[str, str]
) -> None:
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(_FAKE_DOCKER, newline="\n")
    docker.chmod(0o755)
    parent = tmp_path / "docker-build-evidence" / "base-package-inventory.json"
    parent.parent.mkdir()
    parent.write_text("parent\n", newline="\n")
    if step is None:
        script = _ROOT / ".github/scripts/verify_riscv_image_contract.sh"
        argv = [str(script), str(tmp_path), "riscv", "substrate", str(parent)]
    else:
        value = _named_step(*step)
        script = tmp_path / "step.sh"
        script.write_text(value["run"], encoding="utf-8", newline="\n")
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


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under Linux bash")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param(
            {"PUBLISHED_IMAGE": "ghcr.io/acme/base@sha256:aaa", "CANDIDATE_IMAGE": ""},
            ("image=ghcr.io/acme/base@sha256:aaa", "source=published", "candidate-tag="),
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
            ),
            id="candidate",
        ),
    ],
)
def test_selected_base_report(
    tmp_path: Path, env: dict[str, str], expected: tuple[str, ...]
) -> None:
    """The report step names the base it selected, with its digest, in its outputs."""
    step = _step_by_id(_workflow("docker-publish.yml"), "runtime-base", "selected")
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8", newline="\n")
    output = tmp_path / "output"
    result = run_bounded_child(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "CANDIDATE_TAG": "",
            **env,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == list(expected)
