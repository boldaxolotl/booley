"""Behavioral checks for the CI-owned public-demo contract."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from booley.criteria.templates import CriteriaTemplate
from booley.dev_support import demo_contract as demo_contract_module
from booley.dev_support.demo_contract import (
    DemoContract,
    DemoContractError,
    GeneratedInput,
    RequiredBinding,
    _validate_bindings,
    _validate_generated_input,
    load_contract,
)
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.target_contract import TargetContract

CONTRACT = Path(".github/contracts/picorv32-demo.toml")
PREPARE_ACTION = Path(".github/actions/prepare-picorv32-demo/action.yml")
WORKFLOW = Path(".github/workflows/picorv32-demo.yml")
PUBLISH_WORKFLOW = Path(".github/workflows/docker-publish.yml")
EXPORT_SCRIPT = Path(".github/scripts/export_demo_contract.py")
INSTALL_SCRIPT = Path(".github/scripts/install_demo_ticket.py")
VERIFY_SCRIPT = Path(".github/scripts/verify_picorv32_demo.sh")

_PULL_REQUEST_PATHS = {
    ".github/actions/prepare-picorv32-demo/**",
    ".github/contracts/picorv32-demo.toml",
    ".github/contracts/picorv32-demo-ticket.md",
    ".github/scripts/export_demo_contract.py",
    ".github/scripts/install_demo_ticket.py",
    ".github/scripts/picorv32_demo_contract.py",
    ".github/scripts/verify_picorv32_demo.sh",
    ".github/workflows/picorv32-demo.yml",
    "pyproject.toml",
    "src/booley/**",
}


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

    assert set(events["pull_request"]["paths"]) == _PULL_REQUEST_PATHS
    assert events["push"] == {"branches": ["main"]}
    assert events["merge_group"] is None
    assert events["schedule"] == [{"cron": "23 3 * * *"}]
    assert events["workflow_dispatch"] is None


def test_repository_demo_contract_is_pinned_to_public_project_main() -> None:
    raw_contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    contract = load_contract(CONTRACT)

    assert "project_contract_ref" not in raw_contract
    assert len(contract.upstream_ref) == 40
    assert contract.project_ref == "da79489482a7bed69e275ba2c46358ea6636af4d"
    assert contract.ticket_fixture == ".github/contracts/picorv32-demo-ticket.md"
    assert contract.ticket_slug == "add-opt-in-rv32-zbb-pcpi-co-processor"
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

    fixture = Path(contract.ticket_fixture)
    fields, _body = parse_frontmatter(fixture.read_text(encoding="utf-8"))
    sealed = TargetContract.from_mapping(fields["target_contract"])
    assert sealed.schema == 4
    assert sealed.project_sha == contract.project_ref
    assert sealed.outer_sha == contract.upstream_ref
    assert len(sealed.participants) == 2
    assert sealed.surface_entries
    assert sealed.targets == ("lint_core", "sim_core", "sim_wb", "synth_core")
    project = next(item for item in sealed.participants if item.role == "project")
    assert project.ticket_ref == "refs/heads/main"
    assert project.destination_ref == "refs/heads/main"
    assert project.destination_sha == contract.project_ref

    serialized = fixture.read_text(encoding="utf-8")
    assert "ci/agent-ticket-contract" not in serialized
    assert "synth_core_zbb" not in serialized
    assert "sim_zbb_disabled" not in serialized


def test_repository_demo_ticket_uses_current_criteria_grammar() -> None:
    contract = load_contract(CONTRACT)
    fixture = Path(contract.ticket_fixture)
    fields, _body = parse_frontmatter(fixture.read_text(encoding="utf-8"))

    template = CriteriaTemplate.from_yaml(fields["criteria"])
    synthesis = next(spec for spec in template.specs if spec.name == "synthesis_ok")

    assert synthesis.params["cell_count_increase_at_most"] == 11
    assert synthesis.params["critical_path_ps_increase_at_most"] == 3


def test_contract_rejects_scalar_required_targets(tmp_path: Path) -> None:
    path = tmp_path / "contract.toml"
    path.write_text(
        """schema = 1
upstream_repository = "owner/upstream"
upstream_ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
project_repository = "owner/project"
project_ref = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ticket_slug = "demo"
required_targets = "sim"
""",
        encoding="utf-8",
    )

    with pytest.raises(DemoContractError, match="required_targets"):
        load_contract(path)


def test_shared_action_reads_repository_and_revision_pins_from_contract() -> None:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    action = PREPARE_ACTION.read_text(encoding="utf-8")
    workflow_paths = (WORKFLOW, PUBLISH_WORKFLOW)

    for key in (
        "upstream_repository",
        "upstream_ref",
        "project_repository",
        "project_ref",
        "ticket_fixture",
        "ticket_slug",
    ):
        assert contract[key] not in action
        assert f"outputs.{key}" in action
    assert "${GITHUB_WORKSPACE}/.github/scripts/" in action
    assert "${GITHUB_ACTION_PATH}/../.." not in action
    assert "Apply documented local checkout excludes" in action
    assert "'/.booley_project'" in action
    assert "'/.booley-projected-*.core'" in action
    assert action.index("Check out reviewed demo-project revision") < action.index(
        "Apply documented local checkout excludes"
    )
    assert action.index("Apply documented local checkout excludes") < action.index(
        "Require reviewed revisions on public refs"
    )
    assert "git -C demo/.booley_project merge-base --is-ancestor HEAD origin/main" in action
    assert "PROJECT_CONTRACT_REF" not in action
    assert "ci/agent-ticket-contract" not in action

    action_reference = "uses: ./.github/actions/prepare-picorv32-demo"
    verifier = f"bash /booley-source/{VERIFY_SCRIPT.as_posix()}"
    for workflow_path in workflow_paths:
        workflow = workflow_path.read_text(encoding="utf-8")
        assert action_reference in workflow
        assert verifier in workflow
        assert "Check out reviewed PicoRV32 revision" not in workflow
        assert "Install CI-owned Ticket fixture" not in workflow
        assert "picorv32_demo_contract.py" not in _workflow_commands(workflow_path)


def test_release_validation_skips_credentials_and_cannot_promote() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert 'cp -a demo "${RUNNER_TEMP}/booley-picorv32-demo"' in workflow
    assert "working-directory: ${{ runner.temp }}/booley-picorv32-demo" in workflow
    assert "set -o pipefail" in workflow
    assert "booley init --skip-credentials | tee" in workflow
    assert "OPENAI_API_KEY: ci-presence-check-only" not in workflow
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in workflow


def _demo_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".git" / "info").mkdir(parents=True)
    (project / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    return project


def _run_installer(project: Path, fixture: Path, slug: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(INSTALL_SCRIPT),
            "--project-dir",
            str(project),
            "--fixture",
            str(fixture),
            "--slug",
            slug,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_ticket_installer_requires_ticket_free_checkout(tmp_path: Path) -> None:
    project = _demo_project(tmp_path)
    queue = project / "tickets" / "board" / "queue"
    queue.mkdir(parents=True)
    existing = queue / "existing.md"
    existing.write_text("existing\n", encoding="utf-8")
    fixture = tmp_path / "fixture.md"
    fixture.write_text("fixture\n", encoding="utf-8")

    result = _run_installer(project, fixture, "demo")

    assert result.returncode == 2
    assert "already contains queued Tickets: existing.md" in result.stderr
    assert existing.read_text(encoding="utf-8") == "existing\n"
    assert not (queue / "demo.md").exists()


def test_ticket_installer_installs_fixture_into_empty_checkout(tmp_path: Path) -> None:
    project = _demo_project(tmp_path)
    fixture = tmp_path / "fixture.md"
    fixture.write_text("fixture\n", encoding="utf-8")

    result = _run_installer(project, fixture, "demo")

    destination = project / "tickets" / "board" / "queue" / "demo.md"
    assert result.returncode == 0
    assert destination.read_bytes() == fixture.read_bytes()
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o644
    exclude = project / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8") == "/tickets/board/queue/demo.md\n"


def test_contract_exporter_emits_all_workflow_fields() -> None:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--contract", str(CONTRACT)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert dict(line.split("=", 1) for line in result.stdout.splitlines()) == {
        key: contract[key]
        for key in (
            "upstream_repository",
            "upstream_ref",
            "project_repository",
            "project_ref",
            "ticket_fixture",
            "ticket_slug",
        )
    }


def test_required_binding_detects_scalar_mutation_criterion() -> None:
    bindings = (RequiredBinding("criteria.optional.mutation_score", "sim_core"),)
    fields = {"criteria": {"optional": {"mutation_score": "14/15"}}}

    assert _validate_bindings(fields, bindings) == [
        "ticket is missing required binding criteria.optional.mutation_score -> sim_core"
    ]


def test_required_binding_accepts_structured_campaign() -> None:
    bindings = (RequiredBinding("criteria.optional.mutation_score", "sim_core"),)
    fields = {
        "criteria": {
            "optional": {
                "mutation_score": [
                    {
                        "target": "sim_core",
                        "scope": ["picorv32.v"],
                        "min_detected": 14,
                        "total": 15,
                    }
                ]
            }
        }
    }

    assert _validate_bindings(fields, bindings) == []


@pytest.mark.parametrize("scope", [["firmware/ [new]"], ["firmware/**"]])
def test_generated_input_rejects_scope_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: list[str],
) -> None:
    artifact = tmp_path / "firmware" / "firmware.hex"
    artifact.parent.mkdir()
    artifact.write_text("firmware\n", encoding="utf-8")
    producer = tmp_path / "hooks" / "post-setup.sh"
    producer.parent.mkdir()
    producer.write_text("#!/bin/sh\n", encoding="utf-8")
    generated = GeneratedInput(
        path="firmware/firmware.hex",
        producer="hooks/post-setup.sh",
        targets=("sim_core",),
    )

    def git_result(
        _repository: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check
        return subprocess.CompletedProcess(args, 0 if args[0] == "check-ignore" else 1, "", "")

    monkeypatch.setattr(demo_contract_module, "_git", git_result)
    monkeypatch.setattr(
        demo_contract_module.fusesoc_registry,
        "target_referenced_files",
        lambda _root, _target: (generated.path,),
    )

    errors, _path, _digest = _validate_generated_input(tmp_path, scope, generated)

    assert errors == ["generated input must not be ticket Scope: firmware/firmware.hex"]


def _write_contract(tmp_path: Path, replacement: tuple[str, str] | None = None) -> Path:
    text = """schema = 1
upstream_repository = "owner/upstream"
upstream_ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
project_repository = "owner/project"
project_ref = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ticket_fixture = ".github/contracts/ticket.md"
ticket_slug = "demo"
required_targets = ["sim"]

[[required_binding]]
criterion = "criteria.mandatory.sim_pass"
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


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (("schema = 1", "schema = 2"), "schema must be 1"),
        (("owner/upstream", "   "), "upstream_repository must be a non-empty string"),
        (("a" * 40, "ABC"), "upstream_ref must be a full lowercase Git commit SHA"),
        ((".github/contracts/ticket.md", "../ticket.md"), "ticket_fixture"),
        (('required_targets = ["sim"]', "required_targets = []"), "required_targets"),
        (("[[required_binding]]", "[[other_binding]]"), "required_binding"),
        (('criterion = "criteria.mandatory.sim_pass"', "criterion = 7"), "required_binding[0]"),
        (("[[generated_input]]", "[[other_input]]"), "generated_input"),
        (
            ('producer = "Makefile"\ntargets = ["sim"]', 'producer = "Makefile"\ntargets = []'),
            "generated_input[0].targets",
        ),
    ],
)
def test_contract_rejects_invalid_boundary_values(
    tmp_path: Path,
    replacement: tuple[str, str],
    message: str,
) -> None:
    with pytest.raises(DemoContractError, match=message.replace("[", r"\[")):
        load_contract(_write_contract(tmp_path, replacement))


@pytest.mark.parametrize("contents", ["not = [toml", None])
def test_contract_reports_unreadable_or_malformed_input(
    tmp_path: Path, contents: str | None
) -> None:
    path = tmp_path / "contract.toml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    with pytest.raises(DemoContractError, match="cannot read demo contract"):
        load_contract(path)


def test_checkout_ticket_and_fixture_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_result = subprocess.CompletedProcess(["git"], 0, "abc\n", "")
    monkeypatch.setattr(demo_contract_module, "_git", lambda *_args, **_kwargs: git_result)

    demo_contract_module._require_checkout_ref(tmp_path, "abc", "upstream")
    with pytest.raises(DemoContractError, match="expected def"):
        demo_contract_module._require_checkout_ref(tmp_path, "def", "upstream")
    assert demo_contract_module._status(tmp_path) == "abc"

    project = tmp_path / "project"
    (project / "tickets" / "board" / "queue").mkdir(parents=True)
    with pytest.raises(DemoContractError, match="ticket 'demo' is missing"):
        demo_contract_module._ticket_fields(project, "demo")
    ticket = project / "tickets" / "board" / "queue" / "demo.md"
    ticket.write_text("---\nsummary: Demo\n---\n\nBody\n", encoding="utf-8")
    fields, found = demo_contract_module._ticket_fields(project, "demo")
    assert fields["summary"] == "Demo"
    assert found == ticket

    contract_path = tmp_path / "repo" / ".github" / "contracts" / "contract.toml"
    contract_path.parent.mkdir(parents=True)
    fixture_name = ".github/contracts/ticket.md"
    assert demo_contract_module._validate_ticket_fixture(contract_path, fixture_name, ticket) == [
        f"CI-owned ticket fixture is missing: {fixture_name}"
    ]
    fixture = tmp_path / "repo" / fixture_name
    fixture.write_text("different\n", encoding="utf-8")
    assert (
        "does not match"
        in demo_contract_module._validate_ticket_fixture(contract_path, fixture_name, ticket)[0]
    )
    fixture.write_bytes(ticket.read_bytes())
    assert demo_contract_module._validate_ticket_fixture(contract_path, fixture_name, ticket) == []


def test_target_validation_handles_future_missing_invalid_and_broken_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_root: Path, target: str) -> tuple[str, ...]:
        return ("future.sv",) if target == "future" else ()

    def resolve(target: str, **_kwargs: object) -> SimpleNamespace:
        if target == "broken":
            raise demo_contract_module.fusesoc_registry.FuseSocError("cannot resolve")
        return SimpleNamespace(toplevel="" if target == "empty" else "top")

    monkeypatch.setattr(demo_contract_module.fusesoc_registry, "missing_target_sources", missing)
    monkeypatch.setattr(demo_contract_module.fusesoc_registry, "resolve_target", resolve)

    errors = demo_contract_module._validate_targets(
        tmp_path,
        {"scope": ["future.sv [new]"]},
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
        demo_contract_module.fusesoc_registry, "target_referenced_files", referenced
    )

    errors, path, digest = _validate_generated_input(tmp_path, ["build/**"], generated)

    assert path == generated.path
    assert digest == ""
    assert errors == [
        "generated input was not prepared: build/image.hex",
        "generated input must not be ticket Scope: build/image.hex",
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

    errors, digests = demo_contract_module._validate_generated_inputs(
        tmp_path, {"scope": ["future.sv [new]"]}, items
    )

    assert errors == ["bad one"]
    assert digests == {"one.hex": "abc"}


def _demo_contract() -> DemoContract:
    return DemoContract(
        schema=1,
        upstream_repository="owner/upstream",
        upstream_ref="a" * 40,
        project_repository="owner/project",
        project_ref="b" * 40,
        ticket_fixture=".github/contracts/ticket.md",
        ticket_slug="demo",
        required_targets=("sim",),
        required_bindings=(RequiredBinding("criteria.mandatory.sim_pass", "sim"),),
        generated_inputs=(GeneratedInput("image.hex", "Makefile", ("sim",)),),
    )


def test_validate_demo_aggregates_readiness_and_idempotence_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_contract_module, "load_contract", lambda _path: _demo_contract())
    monkeypatch.setattr(demo_contract_module, "_require_checkout_ref", lambda *_args: None)
    monkeypatch.setattr(
        demo_contract_module, "_ticket_fields", lambda *_args: ({}, tmp_path / "t")
    )
    statuses = iter(["", "", "dirty", ""])
    monkeypatch.setattr(demo_contract_module, "_status", lambda _root: next(statuses))
    monkeypatch.setattr(
        demo_contract_module, "_validate_ticket_fixture", lambda *_args: ["fixture"]
    )
    readiness = iter([SimpleNamespace(errors=["first"]), SimpleNamespace(errors=["second"])])
    monkeypatch.setattr(demo_contract_module, "check_ticket_ready", lambda *_args: next(readiness))
    monkeypatch.setattr(demo_contract_module, "_validate_targets", lambda *_args: ["target"])
    monkeypatch.setattr(demo_contract_module, "_validate_bindings", lambda *_args: ["binding"])
    generated = iter([(["generated"], {"image.hex": "one"}), (["again"], {"image.hex": "two"})])
    monkeypatch.setattr(
        demo_contract_module,
        "_validate_generated_inputs",
        lambda *_args: next(generated),
    )

    errors = demo_contract_module.validate_demo(tmp_path / "c.toml", tmp_path, tmp_path)

    assert errors == [
        "fixture",
        "first",
        "target",
        "binding",
        "generated",
        "second preparation: second",
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
