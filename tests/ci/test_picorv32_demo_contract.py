"""Behavioral checks for the CI-owned public-demo contract."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from booley.goals.model import parse_goal_arg

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
    bindings = contract["required_goal"]
    generated_inputs = contract["generated_input"]
    return {
        *contract["required_targets"],
        *(binding["target"] for binding in bindings),
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
        "src/booley/ticket_board/**",
        "src/booley/goals/**",
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


def test_demo_uses_and_retains_the_pulled_immutable_image_identity() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["demo-contract"]["steps"]
    resolver = next(
        step for step in steps if step.get("name") == "Pull and resolve RISC-V Sandbox"
    )
    upload = next(step for step in steps if step.get("name") == "Retain RISC-V Sandbox identity")
    verify = next(
        step
        for step in steps
        if step.get("name") == "Verify public demo contract with current Booley source"
    )

    assert resolver["id"] == "runtime-image"
    assert ".github/scripts/pull_image_identity.py" in resolver["run"]
    assert '--image "${IMAGE}"' in resolver["run"]
    assert '--github-output "${GITHUB_OUTPUT}"' in resolver["run"]
    assert '--evidence "${RUNNER_TEMP}/picorv32-demo/image-identity.json"' in resolver["run"]
    assert upload["if"] == "always()"
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert upload["with"] == {
        "name": "picorv32-image-identity-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ runner.temp }}/picorv32-demo/image-identity.json",
        "if-no-files-found": "error",
        "retention-days": 14,
    }
    assert '"${{ steps.runtime-image.outputs.image }}"' in verify["run"]
    assert '"${IMAGE}" bash' not in verify["run"]


def test_repository_demo_contract_is_pinned_to_public_project_main() -> None:
    raw_contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    contract = load_contract(CONTRACT)

    assert "project_contract_ref" not in raw_contract
    assert len(contract.upstream_ref) == 40
    assert contract.project_ref == "da79489482a7bed69e275ba2c46358ea6636af4d"
    assert contract.schema == 2
    assert all(goal.target in contract.required_targets for goal in contract.required_goals)
    assert contract.toolchain_url.startswith("https://github.com/xpack-dev-tools/")
    assert contract.toolchain_sha256 == (
        "aaaa8060c914851a3e5ee1ba82cc3d6f80972f90638a05c6e823a37557a33758"
    )
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


@pytest.mark.parametrize(
    "path,directory",
    [
        (WORKFLOW, "picorv32-demo"),
        (PUBLISH_WORKFLOW, "picorv32-evidence"),
        (TEST_WORKFLOW, "riscv-image-evidence"),
    ],
)
def test_goal_readiness_evidence_is_mounted_and_retained(path, directory) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    commands = "\n".join(_yaml_strings(workflow))
    if path == TEST_WORKFLOW:
        assert "run_picorv32_ci_demo.sh" in commands
        commands += Path(".github/scripts/run_picorv32_ci_demo.sh").read_text()
    assert "dst=/evidence" in commands
    assert "BOOLEY_GOAL_READINESS_EVIDENCE=/evidence/goal-readiness.json" in commands
    assert directory in commands
    uploads = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert any(
        step.get("if") == "always()" and f"/{directory}/" in step["with"]["path"]
        for step in uploads
    )


def test_contract_rejects_scalar_required_targets(tmp_path: Path) -> None:
    path = tmp_path / "contract.toml"
    path.write_text(
        """schema = 2
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
    assert "'/.booley_project'" in excludes["run"]
    assert "'/.booley-projected-*.core'" in excludes["run"]
    assert (
        "git -C demo/.booley_project merge-base --is-ancestor HEAD origin/main" in ancestry["run"]
    )

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
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(EXPORT_SCRIPT.resolve()),
            "--contract",
            str(contract.resolve()),
        ],
        capture_output=True,
        text=True,
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
    text = """schema = 2
upstream_repository = "owner/upstream"
upstream_ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
project_repository = "owner/project"
project_ref = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
toolchain_url = "https://example.invalid/toolchain.tar.gz"
toolchain_sha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
required_targets = ["sim"]

[[required_goal]]
family = "sim"
target = "sim"

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


_DUPLICATE_BINDING = """target = "sim"

[[required_goal]]
family = "sim"
target = "sim"

[[generated_input]]"""

_INVALID_CONTRACT_CASES = [
    pytest.param(("schema = 2", "schema = 1"), "schema must be 2", id="schema"),
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
    pytest.param(("[[required_goal]]", "[[other_binding]]"), "required_goal", id="bindings"),
    pytest.param(
        ('family = "sim"', "family = 7"),
        "required_goal[0].family",
        id="malformed-binding",
    ),
    pytest.param(
        ('family = "sim"', 'family = " "'),
        "required_goal[0].family must be a non-empty string",
        id="empty-binding",
    ),
    pytest.param(
        ('target = "sim"', 'target = " "'),
        "required_goal[0].target must be a non-empty string",
        id="empty-binding-target",
    ),
    pytest.param(
        ('target = "sim"', 'target = "other"'),
        "required_goal[0].target 'other' is not in required_targets",
        id="out-of-set-binding",
    ),
    pytest.param(
        ('target = "sim"\n\n[[generated_input]]', _DUPLICATE_BINDING),
        "required_goal contains duplicate pair",
        id="duplicate-binding",
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
        schema=2,
        upstream_repository="owner/upstream",
        upstream_ref="a" * 40,
        project_repository="owner/project",
        project_ref="b" * 40,
        toolchain_url="https://example.invalid/toolchain.tar.gz",
        toolchain_sha256="c" * 64,
        required_targets=("sim",),
        required_goals=(parse_goal_arg({"family": "sim", "target": "sim"}),),
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
        "--project-dir",
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


@pytest.mark.parametrize(
    "field", ["optional = true", "criterion = 'LINT'", "min_detected = 0", "auto = false"]
)
def test_goal_contract_rejects_legacy_and_invalid_mutation_fields(tmp_path, field) -> None:
    path = _write_contract(tmp_path, ('family = "sim"', f'family = "mutation"\n{field}'))
    with pytest.raises(DemoContractError, match=r"unknown fields|positive|true"):
        load_contract(path)


def test_contract_goals_use_real_entry_grammar_and_keep_mutation() -> None:
    from booley.goals.translate import translate_goals

    contract = load_contract(CONTRACT)
    goals = translate_goals(contract.required_goals).goals
    assert {goal.key for goal in goals} == {
        "lint_clean_lint_core",
        "sim_pass_sim_core",
        "sim_pass_sim_wb",
        "synthesis_ok_synth_core",
        "mutation_score_sim_core",
    }
    mutation = next(goal for goal in goals if goal.family.value == "mutation")
    assert mutation.params["min_detected"] == 14 and mutation.params["total"] == 15


@pytest.mark.parametrize("field", ["family", "target"])
def test_goal_contract_uses_entry_boundary_normalization(tmp_path, field):
    original = 'family = "sim"' if field == "family" else 'target = "sim"'
    path = _write_contract(tmp_path, (original, original.replace('"sim"', '" sim "')))
    goals = load_contract(path).required_goals
    assert goals == (parse_goal_arg({"family": "sim", "target": "sim"}),)


def test_typed_goals_encode_for_mcp_without_sharing_nested_maps():
    from booley.goals.model import goal_arg_to_json

    for goal in load_contract(CONTRACT).required_goals:
        encoded = goal_arg_to_json(goal)
        assert parse_goal_arg(encoded) == goal
        if "thresholds" in encoded:
            encoded["thresholds"]["cell_count_max"] = 999
            assert "cell_count_max" not in goal.thresholds
