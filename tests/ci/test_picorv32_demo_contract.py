"""Behavioral checks for the CI-owned public-demo contract."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

from booley.runtime import dockerfile_syntax
from booley.runtime.docker_base_contract import stable_base_inputs

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))

from picorv32_ci_inputs import PICORV32_INPUT_FILES

from booley.dev_support import demo_contract as demo_contract_module
from booley.dev_support.demo_contract import (
    DemoContract,
    DemoContractError,
    GeneratedInput,
    _validate_generated_input,
    load_contract,
)

CONTRACT = Path(".github/contracts/picorv32-demo.toml")
PREPARE_ACTION = Path(".github/actions/prepare-picorv32-demo/action.yml")
WORKFLOW = Path(".github/workflows/picorv32-demo.yml")
PUBLISH_WORKFLOW = Path(".github/workflows/docker-publish.yml")
TEST_WORKFLOW = Path(".github/workflows/test.yml")
EXPORT_SCRIPT = Path(".github/scripts/export_demo_contract.py")
VERIFY_SCRIPT = Path(".github/scripts/verify_picorv32_demo.sh")
OUTPUT_KEYS = (
    "upstream_repository",
    "upstream_ref",
    "project_repository",
    "project_ref",
    "toolchain_url",
    "toolchain_sha256",
)


def _yaml_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(item for child in value.values() for item in _yaml_strings(child))
    if isinstance(value, list):
        return tuple(item for child in value for item in _yaml_strings(child))
    return (value,) if isinstance(value, str) else ()


def _contract_table_strings(contract: dict[str, Any]) -> set[str]:
    metrics = contract["target_metrics"]
    generated_inputs = contract["generated_input"]
    return {
        *contract["required_targets"],
        *metrics,
        *(generated[key] for generated in generated_inputs for key in ("path", "producer")),
        *(target for generated in generated_inputs for target in generated["targets"]),
    }


def _workflow_consumers() -> list[tuple[Path, str, list[dict[str, Any]], int]]:
    consumers: list[tuple[Path, str, list[dict[str, Any]], int]] = []
    for path in Path(".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                if step.get("uses") == "./.github/actions/prepare-picorv32-demo":
                    consumers.append((path, job_name, steps, index))
    return consumers


def _workflow_events() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver interprets the unquoted Actions key ``on`` as
    # boolean true. GitHub correctly treats the source key as the string "on".
    return workflow[True]


def _workflow_commands(path: Path) -> str:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return "\n".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    )


def test_pull_requests_run_demo_only_for_its_real_inputs() -> None:
    events = _workflow_events()
    paths = set(events["pull_request"]["paths"])

    assert paths >= PICORV32_INPUT_FILES
    assert ".github/actions/prepare-picorv32-demo/**" in paths
    assert "src/booley/**" not in paths
    assert {
        "src/booley/dev_support/demo_contract.py",
        "src/booley/feedback/**",
        "src/booley/harness/**",
        "src/booley/projects/**",
        "src/booley/runtime/**",
        "src/booley/targets/**",
        "src/booley/review/**",
        "src/booley/evidence/**",
    } <= paths
    assert paths.isdisjoint(
        {
            "src/booley/bwave/**",
            "src/booley/docker/**",
        }
    )
    assert events["push"] == {"branches": ["main"]}
    assert events["merge_group"] is None
    assert events["schedule"] == [{"cron": "23 3 * * *"}]
    assert events["workflow_dispatch"] is None


def _demo_job(name: str = "demo-contract") -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][name]
    assert isinstance(job, dict)
    return job


def _demo_step(name: str) -> dict[str, Any]:
    steps = _demo_job()["steps"]
    return next(step for step in steps if step.get("name") == name)


def _glob_matches(pattern: str, path: str) -> bool:
    """Match a workflow ``paths`` entry: an exact file, or a ``dir/**`` prefix."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    assert "*" not in pattern, pattern
    return path == pattern


def test_demo_never_runs_on_the_released_riscv_image() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "booley-sandbox-riscv" not in text, "workflow still references booley-sandbox-riscv"

    job = _demo_job()
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    verify = steps[names.index("Run measurement round trip on the public demo")]
    build = steps[names.index("Build RISC-V Sandbox candidate")]
    select = steps[names.index("Select compatible stable runtime base")]

    # The demo runs in the candidate the build produced, after the base choice.
    assert "booley-riscv-test bash" in verify["run"]
    # The demo's isolation and project directory are unchanged.
    assert "--network none" in verify["run"]
    assert "-e BOOLEY_DIR=/work/.booley" in verify["run"]
    assert not re.search(r"dst=/booley(?![-\w])", verify["run"])
    assert ".github/scripts/riscv_candidate.sh build" in build["run"]
    assert steps.index(select) < steps.index(build) < steps.index(verify)
    # A failed build fails the check: nothing may continue past it, and no
    # build may write the shared cache (the Tests workflow stays its writer).
    assert not any(step.get("continue-on-error") for step in steps)
    assert not [step for step in steps if "cache-to" in yaml.safe_dump(step)]
    # A failed base selection fails the check instead of skipping it.
    assert str(job["if"]).startswith("${{ !cancelled() && (")
    assert steps[0]["name"] == "Require RISC-V Sandbox base selection"
    assert steps[0]["if"] == "needs.base-plan.result != 'success'"
    assert "exit 1" in steps[0]["run"]


_FAKE_IDENTITY_DOCKER = """#!/usr/bin/env bash
# docker image inspect --format '{{.Id}}' <tag>: a distinct ID per tag.
case "$*" in
  "image inspect --format {{.Id}} booley-riscv-test") echo sha256:%(candidate)s ;;
  "image inspect --format {{.Id}} booley-runtime-base:ci") echo sha256:%(built_base)s ;;
  *) echo "unexpected docker $*" >&2; exit 9 ;;
esac
"""


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under bash")
def test_demo_evidence_names_candidate_digest_and_base_source(tmp_path: Path) -> None:
    step = next(
        (
            step
            for step in _demo_job()["steps"]
            if step.get("name") == "Record RISC-V Sandbox candidate identity"
        ),
        None,
    )
    assert step is not None, "no step records the candidate image identity"
    candidate, built_base = "c" * 64, "b" * 64
    published = "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "a" * 64
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(_FAKE_IDENTITY_DOCKER % {"candidate": candidate, "built_base": built_base})
    docker.chmod(0o755)
    runner_temp = tmp_path / "runner"
    evidence = runner_temp / "picorv32-image-evidence"
    evidence.mkdir(parents=True)
    (evidence / "tooling-source.json").write_text('{"source": "registry"}\n', encoding="utf-8")

    def record(build: str, base_image: str) -> dict[str, Any]:
        result = run_bounded_child(
            ["bash", "-e", "-c", step["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
                "BUILD_STABLE_BASE": build,
                "BASE_IMAGE": base_image,
                "SOURCE_REVISION": "f" * 40,
            },
            check=False,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        assert result.returncode == 0, result.stderr
        return json.loads((evidence / "image-identity.json").read_text(encoding="utf-8"))

    from_published = record("false", published)
    assert from_published["image_id"] == f"sha256:{candidate}"
    assert (tmp_path / "output.txt").read_text(
        encoding="utf-8"
    ) == f"image_id=sha256:{candidate}\n"
    assert from_published["base_source"] == "published"
    assert from_published["base_image"] == published
    assert from_published["base_digest"] == "sha256:" + "a" * 64
    assert from_published["riscv_tooling_source"] == "registry"
    assert from_published["source_revision"] == "f" * 40

    from_source = record("true", "booley-runtime-base:ci")
    assert from_source["base_source"] == "source-build"
    assert from_source["base_digest"] == f"sha256:{built_base}"
    assert from_source["image_id"] == f"sha256:{candidate}"
    assert "source-build" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_demo_retains_image_evidence_even_when_the_build_fails() -> None:
    upload = _demo_step("Retain RISC-V Sandbox identity")
    # Unconditional: the compatibility log of a failed build is evidence too.
    assert upload["if"] == "always()"
    evidence_dir = "${{ runner.temp }}/picorv32-image-evidence/"
    assert upload["with"]["path"] == evidence_dir
    build = _demo_step("Build RISC-V Sandbox candidate")
    assert '"${RUNNER_TEMP}/picorv32-image-evidence"' in build["run"]


def test_stable_base_changes_run_demo_on_a_source_built_base() -> None:
    root = Path(__file__).parents[2]
    patterns = _workflow_events()["pull_request"]["paths"]
    inputs = (
        *stable_base_inputs(root),
        "src/booley/data/docker/stable-base-inputs.txt",
    )
    unmatched = [
        path for path in inputs if not any(_glob_matches(pattern, path) for pattern in patterns)
    ]
    assert not unmatched, f"stable-base inputs that do not trigger the demo: {unmatched}"

    plan = _demo_job("base-plan")
    decide = next(step for step in plan["steps"] if "--publish-decision" in step.get("run", ""))
    assert "ghcr.io/boldaxolotl/booley-sandbox-base:main" in decide["run"]
    assert plan["outputs"]["build"] == f"${{{{ steps.{decide['id']}.outputs.publish }}}}"
    select = _demo_step("Select compatible stable runtime base")
    assert select["env"]["BUILD_STABLE_BASE"] == "${{ needs.base-plan.outputs.build == 'true' }}"
    assert _demo_job()["needs"] == "base-plan"


def test_demo_keeps_90_minute_timeout_unless_it_builds_the_base() -> None:
    timeout = _demo_job()["timeout-minutes"]
    assert isinstance(timeout, str), "the demo timeout has no stable-base build branch"
    match = re.fullmatch(
        r"\$\{\{ needs\.base-plan\.outputs\.build == 'true' && (\d+) \|\| (\d+) \}\}", timeout
    )
    assert match is not None, timeout
    building, reusing = map(int, match.groups())
    assert reusing == 90
    assert building > reusing, "a source-built base needs more than the reuse budget"


def test_repository_demo_contract_is_pinned_to_public_project_main() -> None:
    raw_contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    contract = load_contract(CONTRACT)

    assert "project_contract_ref" not in raw_contract
    assert len(contract.upstream_ref) == 40
    assert contract.project_ref == "a3520837bce9416c1a7209d779148a9eb12482a9"
    assert contract.schema == 3
    assert contract.target_metrics == {
        "lint_core": {"lint": {"errors_max": 0, "track": ["warnings"]}}
    }
    assert set(contract.target_metrics) <= set(contract.required_targets)
    assert contract.toolchain_url.startswith("https://github.com/xpack-dev-tools/")
    assert contract.required_targets == (
        "lint_core",
        "sim_core",
        "sim_dhry",
        "sim_wb",
        "synth_core",
    )
    assert {item.path for item in contract.generated_inputs} == {
        "firmware/firmware.hex",
        "dhrystone/dhry.hex",
    }


def test_demo_toolchain_matches_the_riscv_image_pin() -> None:
    dockerfile = Path("src/booley/data/docker/Dockerfile.riscv").read_text(encoding="utf-8")
    release = dockerfile_syntax.argument_default(dockerfile, "XPACK_GCC_VERSION")
    contract = load_contract(CONTRACT)

    assert contract.toolchain_url == (
        "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/"
        f"v{release}/xpack-riscv-none-elf-gcc-{release}-linux-x64.tar.gz"
    )
    assert contract.toolchain_sha256 == dockerfile_syntax.argument_default(
        dockerfile, "XPACK_GCC_SHA256"
    )


def _assert_measurement_evidence_retained(path: Path, directory: str) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    commands = "\n".join(_yaml_strings(workflow))
    if path == TEST_WORKFLOW:
        assert "run_picorv32_ci_demo.sh" in commands
        commands += Path(".github/scripts/run_picorv32_ci_demo.sh").read_text()
    assert "dst=/evidence" in commands
    assert "BOOLEY_MEASUREMENT_EVIDENCE=/evidence/measurement.json" in commands
    assert directory in commands
    if path == TEST_WORKFLOW:
        steps = workflow["jobs"]["riscv-image"]["steps"]
        upload = next(step for step in steps if step.get("name") == "Upload measurement evidence")
        assert upload["if"] == ("always() && steps.measurement.outputs.started == 'true'")
        assert (
            upload["with"]["path"]
            == "${{ runner.temp }}/riscv-image-evidence/measurement/measurement.json"
        )
        assert upload["with"]["if-no-files-found"] == "error"
        return
    uploads = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    if path == WORKFLOW:
        # The demo also uploads image identity; the measurement upload is the
        # one that keeps this evidence, whatever the verify step did.
        upload = next(
            step for step in uploads if step["with"]["name"].startswith("picorv32-measurement")
        )
        assert str(upload["if"]).startswith("always()")
        assert f"/{directory}/" in upload["with"]["path"]
        return
    assert any(
        step.get("if") == "always()" and f"/{directory}/" in step["with"]["path"]
        for step in uploads
    )


def test_measurement_evidence_is_mounted_and_retained() -> None:
    for path, directory in (
        (WORKFLOW, "picorv32-demo"),
        (PUBLISH_WORKFLOW, "picorv32-evidence"),
        (TEST_WORKFLOW, "riscv-image-evidence"),
    ):
        _assert_measurement_evidence_retained(path, directory)


@pytest.mark.parametrize(
    "path,producer_name,upload_name",
    [
        (
            WORKFLOW,
            "Run measurement round trip on the public demo",
            "Retain measurement evidence",
        ),
        (TEST_WORKFLOW, "Run RISC-V candidate image contract", "Upload measurement evidence"),
    ],
)
def test_measurement_upload_requires_its_producer_to_run(
    path: Path, producer_name: str, upload_name: str
) -> None:
    workflow = yaml.safe_load(path.read_text())
    job = "demo-contract" if path == WORKFLOW else "riscv-image"
    steps = workflow["jobs"][job]["steps"]
    all_steps = [child for step in steps for child in step.get("parallel", [step])]
    producer = next(step for step in all_steps if step.get("name") == producer_name)
    upload = next(step for step in steps if step.get("name") == upload_name)
    assert upload["with"]["if-no-files-found"] == "error"
    if path == WORKFLOW:
        assert producer["id"] == "measurement"
        assert upload["if"] == (
            "always() && (steps.measurement.outcome == 'success' || "
            "steps.measurement.outcome == 'failure')"
        )
    else:
        marker = next(step for step in steps if step.get("id") == "measurement")
        assert marker["name"] == "Read measurement attempt marker"
        assert "measurement-started" in producer["run"]
        assert steps.index(marker) < steps.index(upload)
        assert upload["if"] == ("always() && steps.measurement.outputs.started == 'true'")


def test_release_records_measurement_check_after_validation() -> None:
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "goal-ready" not in text and "goal_readiness" not in text
    workflow = yaml.safe_load(text)
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    records = [step for step in steps if "--check picorv32.measured" in step.get("run", "")]
    assert len(records) == 1
    command = records[0]["run"]
    validator = "python .github/scripts/release_validation/measurement_evidence.py"
    assert validator in command
    assert command.index(validator) < command.index(
        "python .github/scripts/release_validation/evidence.py"
    )
    validation = command[: command.index("python .github/scripts/release_validation/evidence.py")]
    assert '--evidence "${RUNNER_TEMP}/picorv32-evidence/measurement.json"' in validation
    assert '--image-digest "${IMAGE_DIGEST}"' in validation
    assert "||" not in validation


def test_contract_rejects_scalar_required_targets(tmp_path: Path) -> None:
    path = tmp_path / "contract.toml"
    path.write_text(
        """schema = 3
upstream_repository = "owner/upstream"
upstream_ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
project_repository = "owner/project"
project_ref = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
toolchain_url = "https://example.invalid/toolchain.tar.gz"
toolchain_sha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
required_targets = "sim"
""",
        encoding="utf-8",
    )

    with pytest.raises(DemoContractError, match="required_targets"):
        load_contract(path)


def test_shared_action_reads_repository_and_revision_pins_from_contract() -> None:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    action = yaml.safe_load(PREPARE_ACTION.read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    exporter = next(step for step in steps if step.get("id") == "contract")
    checkouts = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    materializer = next(
        step
        for step in steps
        if step.get("name") == "Materialize and validate reviewed demo inputs"
    )

    assert len(checkouts) == 2
    assert steps.index(exporter) < min(steps.index(step) for step in checkouts)
    assert "${GITHUB_WORKSPACE}/.github/scripts/export_demo_contract.py" in exporter["run"]
    assert (
        checkouts[0]["with"]["repository"] == "${{ steps.contract.outputs.upstream_repository }}"
    )
    assert checkouts[0]["with"]["ref"] == "${{ steps.contract.outputs.upstream_ref }}"
    assert checkouts[1]["with"]["repository"] == "${{ steps.contract.outputs.project_repository }}"
    assert checkouts[1]["with"]["ref"] == "${{ steps.contract.outputs.project_ref }}"
    assert materializer["env"] == {
        "CONTRACT_PATH": "${{ inputs.contract }}",
        "PYTHONPATH": "${{ github.workspace }}/src",
    }
    assert materializer["if"] == "inputs.materialize == 'true'"
    assert "picorv32_demo_contract.py" in materializer["run"]

    excludes = next(
        step for step in steps if step.get("name") == "Apply documented local checkout excludes"
    )
    ancestry = next(
        step
        for step in steps
        if step.get("name") == "Require reviewed revisions and materialize destination refs"
    )
    assert steps.index(checkouts[1]) < steps.index(excludes) < steps.index(ancestry)
    assert "'/.booley'" in excludes["run"]
    assert "'/.booley-projected-*.core'" in excludes["run"]
    assert "git -C demo/.booley merge-base --is-ancestor HEAD origin/main" in ancestry["run"]

    action_strings = _yaml_strings(action)
    references = {
        match.group(1)
        for value in action_strings
        for match in re.finditer(r"steps\.contract\.outputs\.([a-z0-9_]+)", value)
    }
    assert references == set(OUTPUT_KEYS)
    assert all(contract[key] not in value for key in OUTPUT_KEYS for value in action_strings)
    assert all(
        authority_value not in value
        for authority_value in _contract_table_strings(contract)
        for value in action_strings
    )
    assert "PROJECT_CONTRACT_REF" not in action_strings
    assert "ci/agent-ticket-contract" not in action_strings
    toolchain = next(
        step for step in steps if step.get("name") == "Install host RISC-V preparation toolchain"
    )
    assert toolchain["env"] == {
        "TOOLCHAIN_URL": "${{ steps.contract.outputs.toolchain_url }}",
        "TOOLCHAIN_SHA256": "${{ steps.contract.outputs.toolchain_sha256 }}",
    }
    assert "curl --proto '=https' --tlsv1.2 -fsSL" in toolchain["run"]
    assert "sha256sum -c -" in toolchain["run"]


def test_workflow_consumers_share_goal_preparation() -> None:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    consumers = _workflow_consumers()
    assert {path for path, _job, _steps, _index in consumers} == {
        WORKFLOW,
        PUBLISH_WORKFLOW,
        TEST_WORKFLOW,
    }
    assert len(consumers) == 5

    for path, _job_name, steps, index in consumers:
        consumer = steps[index]
        consumer_id = consumer.get("id")
        later_strings = _yaml_strings(steps[index + 1 :])
        if consumer_id is not None:
            assert "outputs.ticket_slug" not in "\n".join(later_strings)
        workflow_strings = _yaml_strings(yaml.safe_load(path.read_text(encoding="utf-8")))
        for key in OUTPUT_KEYS[:4]:
            assert all(contract[key] not in value for value in workflow_strings)
        assert all(
            authority_value not in value
            for authority_value in _contract_table_strings(contract)
            for value in workflow_strings
        )
        commands = _workflow_commands(path)
        if path in {WORKFLOW, PUBLISH_WORKFLOW}:
            assert f"bash /booley-source/{VERIFY_SCRIPT.as_posix()}" in commands
        assert "picorv32_demo_contract.py" not in commands
        assert "Check out reviewed PicoRV32 revision" not in workflow_strings
        assert "Install CI-owned Ticket fixture" not in workflow_strings


def test_release_validation_skips_credentials_and_cannot_promote() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    host_validation = next(
        step
        for step in jobs["host-doctor-runtime"]["steps"]
        if step.get("name") == "Run isolated host validation"
    )
    validation_commands = "\n".join(
        str(step.get("run", ""))
        for name, job in jobs.items()
        if name != "promote"
        for step in job["steps"]
    )

    assert "release_validation/host_doctor.py" in host_validation["run"]
    assert "OPENAI_API_KEY" not in validation_commands
    assert "imagetools create" not in validation_commands
    assert jobs["promote"]["if"] == (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    )


def _run_exporter(contract: Path) -> subprocess.CompletedProcess[str]:
    return run_bounded_child(
        [
            sys.executable,
            "-I",
            "-S",
            str(EXPORT_SCRIPT.resolve()),
            "--contract",
            str(contract.resolve()),
        ],
        check=False,
        timeout=10,
    )


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).


def test_contract_exporter_emits_all_workflow_fields() -> None:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    result = _run_exporter(CONTRACT)

    assert result.returncode == 0
    outputs = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert tuple(outputs) == OUTPUT_KEYS
    assert outputs == {key: contract[key] for key in OUTPUT_KEYS}


def _write_contract(tmp_path: Path, replacement: tuple[str, str] | None = None) -> Path:
    text = """schema = 3
upstream_repository = "owner/upstream"
upstream_ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
project_repository = "owner/project"
project_ref = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
toolchain_url = "https://example.invalid/toolchain.tar.gz"
toolchain_sha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
required_targets = ["sim"]

[target_metrics.sim.sim]
failures_max = 0

[[generated_input]]
path = "firmware/image.hex"
producer = "Makefile"
targets = ["sim"]
"""
    if replacement is not None:
        text = text.replace(*replacement)
    path = tmp_path / "contract.toml"
    path.write_text(text, encoding="utf-8")
    return path


_MALFORMED_METRICS_CASES = [
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[other_metrics]"),
        "target_metrics must be a mapping",
    ),
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[target_metrics]"),
        "target_metrics must be a non-empty table",
    ),
    (
        ("[target_metrics.sim.sim]", "[target_metrics.other.sim]"),
        "target_metrics.other is not in required_targets",
    ),
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[target_metrics]\nsim = 7"),
        "target_metrics.sim must be a mapping",
    ),
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[target_metrics.sim]"),
        "target_metrics.sim must be a non-empty table",
    ),
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[target_metrics.sim]\nsim = 7"),
        "target_metrics.sim.sim must be a mapping",
    ),
    (
        ("[target_metrics.sim.sim]\nfailures_max = 0", "[target_metrics.sim.sim]"),
        "target_metrics.sim.sim must be a non-empty table",
    ),
]

_INVALID_CONTRACT_CASES = [
    pytest.param(("schema = 3", "schema = 2"), "schema must be 3", id="schema"),
    pytest.param(
        ("owner/upstream", "   "),
        "upstream_repository must be a non-empty string",
        id="blank-repository",
    ),
    pytest.param(
        ("owner/upstream", " owner/upstream"),
        "upstream_repository must be trimmed",
        id="untrimmed-repository",
    ),
    pytest.param(
        ("a" * 40, "ABC"),
        "upstream_ref must be a full lowercase Git commit SHA",
        id="upstream-revision",
    ),
    pytest.param(
        ("b" * 40, "def"),
        "project_ref must be a full lowercase Git commit SHA",
        id="project-revision",
    ),
    pytest.param(
        (
            "https://example.invalid/toolchain.tar.gz",
            "http://example.invalid/toolchain.tar.gz",
        ),
        "toolchain_url",
        id="toolchain-url",
    ),
    pytest.param(("c" * 64, "ABC"), "toolchain_sha256", id="toolchain-digest"),
    pytest.param(
        ('path = "firmware/image.hex"', 'path = "../image.hex"'),
        "generated_input[0].path",
        id="generated-path",
    ),
    pytest.param(
        ('required_targets = ["sim"]', 'required_targets = "sim"'),
        "required_targets",
        id="malformed-targets",
    ),
    pytest.param(
        ('required_targets = ["sim"]', "required_targets = []"),
        "required_targets",
        id="empty-targets",
    ),
    pytest.param(
        ('required_targets = ["sim"]', 'required_targets = [""]'),
        "required_targets[0]",
        id="empty-target",
    ),
    pytest.param(
        ('required_targets = ["sim"]', 'required_targets = [" sim"]'),
        "required_targets[0] must be trimmed",
        id="untrimmed-target",
    ),
    pytest.param(
        ('required_targets = ["sim"]', 'required_targets = ["sim", "sim"]'),
        "required_targets contains duplicate",
        id="duplicate-target",
    ),
    pytest.param(("[[generated_input]]", "[[other_input]]"), "generated_input", id="inputs"),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = [7]',
        ),
        "generated_input[0].targets",
        id="malformed-consumer",
    ),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = []',
        ),
        "generated_input[0].targets",
        id="empty-consumers",
    ),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = [""]',
        ),
        "generated_input[0].targets[0] must be a non-empty string",
        id="empty-consumer",
    ),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = [" sim"]',
        ),
        "generated_input[0].targets[0] must be trimmed",
        id="untrimmed-consumer",
    ),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = ["sim", "sim"]',
        ),
        "generated_input[0].targets contains duplicate",
        id="duplicate-consumer",
    ),
    pytest.param(
        (
            'producer = "Makefile"\ntargets = ["sim"]',
            'producer = "Makefile"\ntargets = ["other"]',
        ),
        "generated_input[0].targets consumer 'other' is not in required_targets",
        id="out-of-set-consumer",
    ),
]


@pytest.mark.parametrize(("replacement", "message"), _INVALID_CONTRACT_CASES)
def test_contract_rejects_invalid_boundary_values(
    tmp_path: Path,
    replacement: tuple[str, str],
    message: str,
) -> None:
    with pytest.raises(DemoContractError, match=re.escape(message)):
        load_contract(_write_contract(tmp_path, replacement))


def test_codec_rejects_malformed_target_metrics(tmp_path: Path) -> None:
    for index, (replacement, message) in enumerate(_MALFORMED_METRICS_CASES):
        directory = tmp_path / str(index)
        directory.mkdir()
        with pytest.raises(DemoContractError, match=re.escape(message)):
            load_contract(_write_contract(directory, replacement))


def test_contract_target_metrics_parse_as_metrics_toml() -> None:
    from release_validation.measure_round_trip import render_metrics

    from booley.measurement.metrics_file import parse_metrics_as_written

    contract = load_contract(CONTRACT)
    rules = parse_metrics_as_written(render_metrics(contract.target_metrics).encode())
    assert {table.target for table in rules.tables} == set(contract.target_metrics)
    assert {table.flow for table in rules.tables} == {"lint"}
    assert set(contract.target_metrics) <= set(contract.required_targets)


@pytest.mark.parametrize(("replacement", "message"), _INVALID_CONTRACT_CASES)
def test_exporter_rejects_invalid_typed_contract_without_outputs(
    tmp_path: Path,
    replacement: tuple[str, str],
    message: str,
) -> None:
    result = _run_exporter(_write_contract(tmp_path, replacement))

    assert result.returncode == 2
    assert result.stdout == ""
    assert message in result.stderr


@pytest.mark.parametrize("contents", ["not = [toml", None])
def test_contract_reports_unreadable_or_malformed_input(
    tmp_path: Path, contents: str | None
) -> None:
    path = tmp_path / "contract.toml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    with pytest.raises(DemoContractError, match="cannot read demo contract"):
        load_contract(path)


def test_target_validation_handles_invalid_and_broken_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_root: Path, target: str) -> tuple[str, ...]:
        return ("future.sv",) if target == "future" else ()

    def resolve(target: str, **_kwargs: object) -> SimpleNamespace:
        if target == "broken":
            raise demo_contract_module.fusesoc_registry.FuseSocError("cannot resolve")
        return SimpleNamespace(toplevel="" if target == "empty" else "top")

    monkeypatch.setattr(
        demo_contract_module,
        "_target_inputs",
        lambda _catalog, target: tuple(
            SimpleNamespace(path=path) for path in missing(tmp_path, target)
        ),
    )
    monkeypatch.setattr(
        demo_contract_module,
        "_resolve_catalog_target",
        lambda _catalog, target, _build_root: resolve(target),
    )

    errors = demo_contract_module._validate_targets(
        tmp_path,
        ("future", "valid", "empty", "broken"),
    )

    assert errors == [
        "required Target 'empty' resolves without a toplevel",
        "required Target 'broken': cannot resolve",
    ]


def test_generated_input_reports_every_policy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = GeneratedInput("build/image.hex", "Makefile", ("broken", "missing-ref"))

    def git_result(
        _repository: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        return subprocess.CompletedProcess(args, 0 if args[0] == "ls-files" else 1, "", "")

    def referenced(_root: Path, target: str) -> tuple[str, ...]:
        if target == "broken":
            raise demo_contract_module.fusesoc_registry.FuseSocError("bad Target")
        return ("other.hex",)

    monkeypatch.setattr(demo_contract_module, "_git", git_result)
    monkeypatch.setattr(
        demo_contract_module,
        "_target_inputs",
        lambda _catalog, target: tuple(
            SimpleNamespace(path=path) for path in referenced(tmp_path, target)
        ),
    )

    errors, path, digest = _validate_generated_input(tmp_path, generated)

    assert path == generated.path
    assert digest == ""
    assert errors == [
        "generated input was not prepared: build/image.hex",
        "generated input must not be committed: build/image.hex",
        "generated input must be ignored: build/image.hex",
        "generated input producer is missing for build/image.hex: Makefile",
        "generated input build/image.hex target 'broken': bad Target",
        "Target 'missing-ref' does not declare generated input build/image.hex",
    ]


def test_generated_input_collection_keeps_only_available_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = (
        GeneratedInput("one.hex", "Makefile", ("sim",)),
        GeneratedInput("two.hex", "Makefile", ("sim",)),
    )
    outcomes = iter([(["bad one"], "one.hex", "abc"), ([], "two.hex", "")])
    monkeypatch.setattr(
        demo_contract_module,
        "_validate_generated_input",
        lambda *_args: next(outcomes),
    )

    errors, digests = demo_contract_module._validate_generated_inputs(tmp_path, items)

    assert errors == ["bad one"]
    assert digests == {"one.hex": "abc"}


def _demo_contract() -> DemoContract:
    return DemoContract(
        schema=3,
        upstream_repository="owner/upstream",
        upstream_ref="a" * 40,
        project_repository="owner/project",
        project_ref="b" * 40,
        toolchain_url="https://example.invalid/toolchain.tar.gz",
        toolchain_sha256="c" * 64,
        required_targets=("sim",),
        target_metrics={"sim": {"sim": {"failures_max": 0}}},
        generated_inputs=(GeneratedInput("image.hex", "Makefile", ("sim",)),),
    )


def test_validate_demo_aggregates_readiness_and_idempotence_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_contract_module, "load_contract", lambda _path: _demo_contract())
    monkeypatch.setattr(demo_contract_module, "_require_checkout_ref", lambda *_args: None)
    statuses = iter(["", "", "dirty", ""])
    monkeypatch.setattr(demo_contract_module, "_status", lambda _root: next(statuses))
    monkeypatch.setattr(demo_contract_module, "_prepare_demo_project", lambda *_args: [])
    monkeypatch.setattr(demo_contract_module, "_validate_targets", lambda *_args: ["target"])
    generated = iter([(["generated"], {"image.hex": "one"}), (["again"], {"image.hex": "two"})])
    monkeypatch.setattr(
        demo_contract_module,
        "_validate_generated_inputs",
        lambda *_args: next(generated),
    )

    errors = demo_contract_module.validate_demo(tmp_path / "c.toml", tmp_path, tmp_path)

    assert errors == [
        "target",
        "generated",
        "second preparation: again",
        "project preparation is not idempotent: generated input digests changed",
        "project preparation changed Git-visible checkout state",
        "demo checkouts are not pristine after preparation",
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DemoContractError("wrong checkout"), "wrong checkout"),
        (
            subprocess.CalledProcessError(3, ["git"], stderr="bad revision\n"),
            "Git inspection failed (rc=3): bad revision",
        ),
    ],
)
def test_validate_demo_reports_checkout_inspection_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    monkeypatch.setattr(demo_contract_module, "load_contract", lambda _path: _demo_contract())

    def fail(*_args: object) -> None:
        raise error

    monkeypatch.setattr(demo_contract_module, "_require_checkout_ref", fail)

    assert demo_contract_module.validate_demo(tmp_path / "c", tmp_path, tmp_path) == [expected]


def test_demo_contract_cli_reports_success_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--contract",
        str(tmp_path / "c"),
        "--demo-root",
        str(tmp_path),
        "--booley-dir",
        str(tmp_path),
    ]
    monkeypatch.setattr(demo_contract_module, "validate_demo", lambda *_args: [])
    assert demo_contract_module.main(args) == 0
    assert "PicoRV32 demo contract passed" in capsys.readouterr().out

    monkeypatch.setattr(
        demo_contract_module,
        "validate_demo",
        lambda *_args: (_ for _ in ()).throw(DemoContractError("bad contract")),
    )
    assert demo_contract_module.main(args) == 2
    assert "ERROR: bad contract" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="verifier executes inside the Linux image")
def test_demo_verifier_uses_explicit_directory_for_both_stages(tmp_path, monkeypatch):
    import json
    import os
    import sys

    commands = tmp_path / "commands.jsonl"
    binary = tmp_path / "bin/python"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['DEMO_COMMAND_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    binary.chmod(0o755)
    configured = "/work/Project inputs/.booley"
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BOOLEY_DIR", configured)
    monkeypatch.setenv("DEMO_COMMAND_LOG", str(commands))
    monkeypatch.setenv("BOOLEY_RUN_PICORV32_FLOWS", "0")
    monkeypatch.setenv("BOOLEY_IMAGE_DIGEST", "sha256:image")
    run_bounded_child(
        ["bash", str(VERIFY_SCRIPT.resolve())],
        check=True,
        timeout=10,
    )
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    assert len(calls) == 2
    for arguments, option in zip(calls, ("--booley-dir", "--project-state"), strict=True):
        assert arguments[arguments.index(option) + 1] == configured


def test_demo_workflow_runs_measurement_round_trip_on_its_inputs() -> None:
    paths = set(_workflow_events()["pull_request"]["paths"])
    inputs = {
        ".github/scripts/release_validation/measure_round_trip.py",
        ".github/scripts/release_validation/measurement_evidence.py",
    }
    assert inputs <= PICORV32_INPUT_FILES and inputs <= paths
    assert "src/booley/measurement/**" in paths
    assert any(_glob_matches(pattern, "src/booley/harness/setup/scaffold.py") for pattern in paths)

    identity = _demo_step("Record RISC-V Sandbox candidate identity")
    assert identity["id"] == "image-identity"
    assert 'echo "image_id=${image_id}" >> "${GITHUB_OUTPUT}"' in identity["run"]
    step = _demo_step("Run measurement round trip on the public demo")
    assert step["id"] == "measurement"
    assert step["env"] == {"BOOLEY_IMAGE_DIGEST": "${{ steps.image-identity.outputs.image_id }}"}
    assert "-e BOOLEY_IMAGE_DIGEST " in step["run"]
    assert "-e BOOLEY_MEASUREMENT_EVIDENCE=/evidence/measurement.json" in step["run"]
    assert "verify_picorv32_demo.sh" in step["run"]

    verify = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert "release_validation/measure_round_trip.py" in verify
    assert '--image-digest "${BOOLEY_IMAGE_DIGEST:?' in verify
    assert '--evidence "${BOOLEY_MEASUREMENT_EVIDENCE:-' in verify


_GOAL_IMPORT_ROOTS = ("booley.criteria", "goal_mode_driver")
_GOAL_FREE_PYTHON = (
    *sorted(Path(".github/scripts/release_validation").glob("*.py")),
    Path(".github/scripts/export_demo_contract.py"),
    Path(".github/scripts/picorv32_demo_contract.py"),
    Path(".github/scripts/picorv32_ci_inputs.py"),
    Path("src/booley/dev_support/demo_contract.py"),
    Path("src/booley/dev_support/demo_contract_codec.py"),
)
_GOAL_FREE_TEXT = (
    *_GOAL_FREE_PYTHON,
    VERIFY_SCRIPT,
    Path(".github/scripts/run_picorv32_ci_demo.sh"),
    CONTRACT,
    PREPARE_ACTION,
    WORKFLOW,
    PUBLISH_WORKFLOW,
    TEST_WORKFLOW,
)


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_release_and_demo_inputs_use_no_goal_mode() -> None:
    offenders = {
        str(path): sorted(
            module
            for module in _imported_modules(path)
            if module.split(".")[0] in {"goal_mode_driver"}
            or any(module == root or module.startswith(f"{root}.") for root in _GOAL_IMPORT_ROOTS)
        )
        for path in _GOAL_FREE_PYTHON
    }
    assert {path: modules for path, modules in offenders.items() if modules} == {}
    named = {
        str(path): re.findall(r"(?i)goal\w*", path.read_text("utf-8")) for path in _GOAL_FREE_TEXT
    }
    assert {path: words for path, words in named.items() if words} == {}
