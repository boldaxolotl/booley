"""Immutable Target contract schema, digest, and criterion validation."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter
from booley.ticket_board.target_contract import (
    ContractParticipant,
    TargetContract,
    TargetContractError,
    build_contract,
    criterion_targets,
    resolve_commit,
    surface_digest,
    validate_contract_fields,
    validate_criterion_targets,
    validate_targets_for_seal,
)

_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:lib:toy:1.0

    filesets:
      rtl:
        files: [rtl/toy.sv]
        file_type: systemVerilogSource
      tb:
        files:
          - tb/toy_tb.sv: {tags: [tb]}
        file_type: systemVerilogSource
      future:
        files: [rtl/future.sv]
        file_type: systemVerilogSource
      constraints:
        files:
          - constraints/toy.sdc: {file_type: SDC}

    targets:
      sim_toy:
        flow: sim
        flow_options: {tool: verilator}
        filesets: [rtl, tb]
        toplevel: toy_tb
      lint_toy:
        flow: lint
        flow_options: {tool: verilator}
        filesets: [rtl]
        toplevel: toy
      synth_before:
        flow: generic
        flow_options: {tool: yosys}
        filesets: [rtl, constraints]
        toplevel: toy
      synth_after:
        flow: generic
        flow_options: {tool: yosys}
        filesets: [rtl, constraints]
        toplevel: toy
      synth_future:
        flow: generic
        flow_options: {tool: yosys}
        filesets: [future, constraints]
        toplevel: future
    """
)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "toy.core").write_text(_CORE, encoding="utf-8")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "toy.sv").write_text("module toy; endmodule\n", encoding="utf-8")
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "toy_tb.sv").write_text("module toy_tb; endmodule\n", encoding="utf-8")
    (tmp_path / "constraints").mkdir()
    (tmp_path / "constraints" / "toy.sdc").write_text(
        "create_clock -period 10 [get_ports clk]\n", encoding="utf-8"
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "tests.toml").write_text("[targets.sim_toy]\ntests = ['all']\n")
    (project / "booley.toml").write_text(
        "[flows.sim]\ndefault_target = 'sim_toy'\n\n[display]\ncolor = true\n"
    )
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _participant(sha: str = "a" * 40) -> ContractParticipant:
    return ContractParticipant(
        "outer",
        sha,
        "refs/heads/ticket",
        "refs/heads/main",
        "b" * 40,
    )


def test_contract_round_trips_as_nested_frontmatter(tmp_path: Path) -> None:
    project = _project(tmp_path)
    contract = build_contract(
        project,
        outer_sha="a" * 40,
        targets=["sim_toy", "lint_toy"],
        participants=[_participant()],
    )
    fields = {
        "summary": "sealed",
        "type": "feature",
        "branch": "main",
        "scope": [],
        "base_sha": contract.outer_sha,
        "target_contract": contract.as_dict(),
    }

    parsed, _body = parse_frontmatter(format_frontmatter(fields, "body"))

    assert TargetContract.from_mapping(parsed["target_contract"]) == contract
    assert validate_contract_fields(parsed) == []


def test_contract_with_bindings_round_trips_as_nested_frontmatter(tmp_path: Path) -> None:
    project = _project(tmp_path)
    criteria = {
        "mandatory": {
            "synthesis_ok": {
                "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                "area_reduce_at_least": 10,
            }
        }
    }
    contract = build_contract(
        project,
        outer_sha="a" * 40,
        targets=["synth_before", "synth_after"],
        bindings=criterion_targets(criteria),
        participants=[_participant()],
    )
    fields = {
        "summary": "sealed",
        "type": "feature",
        "branch": "main",
        "scope": [],
        "base_sha": contract.outer_sha,
        "target_contract": contract.as_dict(),
    }

    parsed, _body = parse_frontmatter(format_frontmatter(fields, "body"))

    assert TargetContract.from_mapping(parsed["target_contract"]) == contract


def test_schema_four_seals_repository_participants_and_surface_entries(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outer = ContractParticipant(
        role="outer",
        sealed_sha="a" * 40,
        ticket_ref="refs/heads/add-target",
        destination_ref="refs/heads/main",
        destination_sha="b" * 40,
    )

    contract = build_contract(
        project,
        outer_sha=outer.sealed_sha,
        targets=["sim_toy"],
        participants=[outer],
    )
    parsed = TargetContract.from_mapping(contract.as_dict())

    assert parsed == contract
    assert parsed.schema == 4
    assert parsed.participants == (outer,)
    assert parsed.surface_entries
    assert {entry.kind for entry in parsed.surface_entries} >= {"core", "target-selection"}


def test_schema_three_rejects_participant_that_disagrees_with_outer_sha() -> None:
    with pytest.raises(TargetContractError, match="outer participant sealed_sha"):
        TargetContract.from_mapping(
            {
                "schema": 3,
                "outer_sha": "a" * 40,
                "project_sha": "",
                "surface_digest": "b" * 64,
                "surface_entries": [],
                "targets": [],
                "bindings": [],
                "participants": [
                    {
                        "role": "outer",
                        "sealed_sha": "c" * 40,
                        "ticket_ref": "refs/heads/ticket",
                        "destination_ref": "refs/heads/main",
                        "destination_sha": "d" * 40,
                    }
                ],
            }
        )


def _schema_three_mapping() -> dict[str, Any]:
    return {
        "schema": 3,
        "outer_sha": "a" * 40,
        "project_sha": "",
        "surface_digest": "b" * 64,
        "surface_entries": [],
        "targets": [],
        "bindings": [],
        "participants": [
            {
                "role": "outer",
                "sealed_sha": "a" * 40,
                "ticket_ref": "refs/heads/ticket",
                "destination_ref": "refs/heads/main",
                "destination_sha": "c" * 40,
            }
        ],
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("participants", None, r"participants must be a list\[dict\]"),
        ("participants", [None], "participants\\[0\\] is malformed"),
        (
            "participants",
            [
                {
                    "role": "worker",
                    "sealed_sha": "a" * 40,
                    "ticket_ref": "refs/heads/ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                }
            ],
            "role must be 'outer' or 'project'",
        ),
        (
            "participants",
            [
                {
                    "role": "outer",
                    "sealed_sha": "short",
                    "ticket_ref": "refs/heads/ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                }
            ],
            "commit identities must be full Git SHAs",
        ),
        (
            "participants",
            [
                {
                    "role": "outer",
                    "sealed_sha": "a" * 40,
                    "ticket_ref": "ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                }
            ],
            "refs must be full refs/heads names",
        ),
        (
            "surface_entries",
            None,
            r"surface_entries must be a list\[dict\]",
        ),
        ("surface_entries", [None], "surface_entries\\[0\\] is malformed"),
        (
            "surface_entries",
            [{"path": "toy.core", "kind": "core", "sha256": "short"}],
            "has an invalid path, kind, or digest",
        ),
    ],
)
def test_schema_three_rejects_malformed_participant_and_surface_rows(
    field: str, value: object, message: str
) -> None:
    mapping = _schema_three_mapping()
    mapping[field] = value

    with pytest.raises(TargetContractError, match=message):
        TargetContract.from_mapping(mapping)


def test_schema_three_requires_sorted_unique_participants_and_surface_entries() -> None:
    participant_mapping = _schema_three_mapping()
    participant_mapping["participants"] *= 2
    with pytest.raises(TargetContractError, match="participants must be sorted and unique"):
        TargetContract.from_mapping(participant_mapping)

    surface_mapping = _schema_three_mapping()
    entry = {"path": "toy.core", "kind": "core", "sha256": "d" * 64}
    surface_mapping["surface_entries"] = [entry, entry]
    with pytest.raises(TargetContractError, match="surface_entries must be sorted and unique"):
        TargetContract.from_mapping(surface_mapping)


@pytest.mark.parametrize(
    ("participants", "project_sha", "message"),
    [
        (
            [
                {
                    "role": "outer",
                    "sealed_sha": "a" * 40,
                    "ticket_ref": "refs/heads/a",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                },
                {
                    "role": "outer",
                    "sealed_sha": "a" * 40,
                    "ticket_ref": "refs/heads/b",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                },
            ],
            "",
            "may contain each role once",
        ),
        (
            [
                {
                    "role": "project",
                    "sealed_sha": "d" * 40,
                    "ticket_ref": "refs/heads/ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                }
            ],
            "d" * 40,
            "requires an outer participant",
        ),
        (_schema_three_mapping()["participants"], "d" * 40, "presence must match project_sha"),
        (
            [
                *_schema_three_mapping()["participants"],
                {
                    "role": "project",
                    "sealed_sha": "e" * 40,
                    "ticket_ref": "refs/heads/ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                },
            ],
            "d" * 40,
            "project participant sealed_sha must equal project_sha",
        ),
    ],
)
def test_schema_three_rejects_inconsistent_participant_set(
    participants: object, project_sha: str, message: str
) -> None:
    mapping = _schema_three_mapping()
    mapping["participants"] = participants
    mapping["project_sha"] = project_sha

    with pytest.raises(TargetContractError, match=message):
        TargetContract.from_mapping(mapping)


def test_contract_rejects_caller_fabricated_base_sha(tmp_path: Path) -> None:
    project = _project(tmp_path)
    contract = build_contract(
        project,
        outer_sha="a" * 40,
        targets=["sim_toy"],
        participants=[_participant()],
    )

    errors = validate_contract_fields(
        {"base_sha": "b" * 40, "target_contract": contract.as_dict()}
    )

    assert errors == ["base_sha must equal target_contract.outer_sha"]


def test_contract_requires_sorted_unique_targets() -> None:
    with pytest.raises(TargetContractError, match="sorted and unique"):
        mapping = _schema_three_mapping()
        mapping["targets"] = ["sim_b", "sim_a", "sim_a"]
        TargetContract.from_mapping(mapping)


def test_paired_targets_bind_candidate_criterion_to_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    criteria = {
        "mandatory": {
            "synthesis_ok": {
                "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                "area_reduce_at_least": 10,
            }
        }
    }

    bindings = criterion_targets(criteria)
    contract = build_contract(
        project,
        outer_sha="a" * 40,
        targets=["synth_before", "synth_after"],
        bindings=bindings,
        participants=[_participant()],
    )

    assert bindings[0].target == "synth_after"
    assert bindings[0].baseline == "synth_before"
    assert contract.schema == 4
    assert contract.bindings[0].baseline == "acme:lib:toy:1.0#synth_before"
    assert contract.bindings[0].candidate == "acme:lib:toy:1.0#synth_after"
    assert contract.bindings[0].baseline_selector == "synth_before"
    assert contract.bindings[0].candidate_selector == "synth_after"


def test_schema_three_binding_codec_round_trips_without_schema_four_fields() -> None:
    encoded = _schema_three_mapping()
    encoded["targets"] = ["synth_after", "synth_before"]
    encoded["bindings"] = [
        {
            "flow": "synth",
            "criterion": "synthesis_ok",
            "baseline": "acme:lib:toy:1.0#synth_before",
            "candidate": "acme:lib:toy:1.0#synth_after",
        }
    ]

    assert TargetContract.from_mapping(encoded).as_dict() == encoded


def test_schema_three_surface_digest_codec_remains_stable(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert surface_digest(project, schema=3) == (
        "73ff7cd9e288c91c39d9987bf7e578ee7cbada8b9135f4349244e9af467e5396"
    )


def test_legacy_contract_schema_is_rejected() -> None:
    fields = {
        "base_sha": "a" * 40,
        "target_contract": {
            "schema": 1,
            "outer_sha": "a" * 40,
            "project_sha": "",
            "surface_digest": "b" * 64,
            "targets": ["synth_after", "synth_before"],
        },
        "criteria": {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                    "area_reduce_at_least": 10,
                }
            }
        },
    }

    assert validate_contract_fields(fields) == [
        "target_contract.schema must be one of [3, 4], got 1"
    ]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "target_contract must be a mapping"),
        (
            {
                "schema": 3,
                "outer_sha": "a" * 40,
                "project_sha": "",
                "surface_digest": "b" * 64,
                "targets": "sim_toy",
                "bindings": [],
                "participants": [],
                "surface_entries": [],
            },
            r"target_contract\.targets must be a list\[str\]",
        ),
    ],
)
def test_contract_boundary_rejects_invalid_shapes(value: object, message: str) -> None:
    with pytest.raises(TargetContractError, match=message):
        TargetContract.from_mapping(value)


def test_surface_ignores_rtl_but_covers_every_control_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = surface_digest(project)
    (project / "rtl" / "toy.sv").write_text("module toy; wire changed; endmodule\n")
    assert surface_digest(project) == original

    core = project / "toy.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace("toplevel: toy\n", "toplevel: toy_v2\n")
    )
    after_core = surface_digest(project)
    assert after_core != original

    tests_config = project / ".booley_project" / "tests.toml"
    tests_config.write_text("[targets.sim_toy]\ntests = ['smoke']\n")
    after_tests = surface_digest(project)
    assert after_tests != after_core

    flow_config = project / ".booley_project" / "booley.toml"
    flow_config.write_text("[flows.sim]\ndefault_target = 'lint_toy'\n")
    after_flow = surface_digest(project)
    assert after_flow != after_tests

    constraint = project / "constraints" / "toy.sdc"
    constraint.write_text("create_clock -period 8 [get_ports clk]\n")
    assert surface_digest(project) != after_flow


def test_surface_uses_checkout_configured_project_directory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    default = project / ".booley_project"
    custom = project / "control"
    default.rename(custom)
    (project / "booley.toml").write_text('[project]\ndir = "control"\n', encoding="utf-8")
    original = surface_digest(project)

    (custom / "tests.toml").write_text("[targets.sim_toy]\ntests = ['smoke']\n")

    assert surface_digest(project) != original


def test_non_target_booley_config_does_not_change_surface(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".booley_project" / "booley.toml"
    original = surface_digest(project)

    config.write_text(config.read_text(encoding="utf-8").replace("true", "false"))

    assert surface_digest(project) == original


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_contract_schemas_are_not_supported(schema: int) -> None:
    mapping = _schema_three_mapping()
    mapping["schema"] = schema

    with pytest.raises(TargetContractError, match=r"schema must be one of \[3, 4\]"):
        TargetContract.from_mapping(mapping)


def test_schema_four_digest_uses_selected_capi2_semantics(tmp_path: Path) -> None:
    literal = tmp_path / "literal"
    conditional = tmp_path / "conditional"
    literal.mkdir()
    conditional.mkdir()
    _project(literal)
    _project(conditional)
    core = conditional / "toy.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            "files: [rtl/toy.sv]",
            'files: ["tool_verilator ? (rtl/toy.sv)"]',
        ),
        encoding="utf-8",
    )

    assert surface_digest(literal, targets=("sim_toy",)) == surface_digest(
        conditional, targets=("sim_toy",)
    )


def test_schema_four_digest_excludes_selected_source_existence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = surface_digest(project, targets=("synth_future",))

    (project / "rtl" / "future.sv").write_text("module future; endmodule\n")

    assert surface_digest(project, targets=("synth_future",)) == original


def test_surface_covers_referenced_core_and_config_hooks(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hooks = project / "hooks"
    hooks.mkdir()
    core_hook = hooks / "prepare.py"
    config_hook = hooks / "select"
    core_hook.write_text("print('prepare')\n")
    config_hook.write_text("#!/bin/sh\nexit 0\n")
    core = project / "toy.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator, pre_run: hooks/prepare.py}",
            1,
        )
    )
    config = project / ".booley_project" / "booley.toml"
    config.write_text("[flows.sim]\ndefault_target = 'sim_toy'\npre_run = 'hooks/select'\n")
    original = surface_digest(project)

    core_hook.write_text("print('changed')\n")
    after_core_hook = surface_digest(project)
    assert after_core_hook != original

    config_hook.write_text("#!/bin/sh\nexit 1\n")
    assert surface_digest(project) != after_core_hook


def test_optional_and_mandatory_targets_use_same_flow_rules(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = {
        "scope": [],
        "criteria": {
            "mandatory": {"sim_pass": ["tb/toy_tb.sv @ sim_toy @ pass -> pass"]},
            "optional": {"synthesis_ok": {"targets": ["lint_toy"]}},
        },
    }

    errors = validate_criterion_targets(fields, project)

    assert any("criteria.optional.synthesis_ok" in error for error in errors)
    assert any("cannot satisfy synthesis_ok" in error for error in errors)


def test_future_nonrelative_target_accepts_only_scope_new_sources(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = {
        "scope": ["rtl/future.sv [new]"],
        "criteria": {"mandatory": {"synthesis_ok": {"targets": ["synth_future"]}}},
    }

    assert validate_criterion_targets(fields, project) == []
    assert validate_targets_for_seal(fields, project, tmp_path / "build") == []

    fields["scope"] = ["rtl/future.sv"]
    errors = validate_criterion_targets(fields, project)
    assert any("not declared Scope [new]" in error for error in errors)


def test_contract_validation_uses_condition_selected_source_paths(tmp_path: Path) -> None:
    (tmp_path / "conditional.core").write_text(
        textwrap.dedent(
            """\
            CAPI=2:
            name: acme:ip:conditional:1.0
            filesets:
              harness:
                files:
                  - tool_verilator ? (ibex_simple_system_main.cc)
                  - tool_icarus ? (unused_main.cc)
            targets:
              sim:
                flow: sim
                flow_options: {tool: verilator}
                filesets: [harness]
                toplevel: ibex_simple_system
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "ibex_simple_system_main.cc").write_text("int main() {}\n", encoding="utf-8")
    fields = {
        "scope": [],
        "criteria": {"mandatory": {"sim_pass": ["sim"]}},
    }

    assert validate_criterion_targets(fields, tmp_path) == []


def test_future_relative_target_requires_executable_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = {
        "scope": ["rtl/future.sv [new]"],
        "criteria": {
            "optional": {
                "synthesis_ok": {
                    "targets": ["synth_future"],
                    "area_um2_increase_at_most": 5,
                }
            }
        },
    }

    bindings = criterion_targets(fields["criteria"])
    errors = validate_criterion_targets(fields, project)

    assert bindings[0].relative is True
    assert any("relative-QoR" in error and "rtl/future.sv" in error for error in errors)


def test_paired_relative_candidate_accepts_scope_new_source(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = {
        "scope": ["rtl/future.sv [new]"],
        "criteria": {
            "optional": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_before", "candidate": "synth_future"}],
                    "area_reduce_at_least": 5,
                }
            }
        },
    }

    assert validate_criterion_targets(fields, project) == []


def test_paired_relative_candidate_rejects_undeclared_missing_source(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = {
        "scope": [],
        "criteria": {
            "optional": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_before", "candidate": "synth_future"}],
                    "area_reduce_at_least": 5,
                }
            }
        },
    }

    errors = validate_criterion_targets(fields, project)

    assert any("candidate target 'synth_future'" in error for error in errors)


def test_resolve_commit_rejects_unresolvable_full_sha(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "initial")
    head = _git(project, "rev-parse", "HEAD")

    assert resolve_commit(project, head) == head
    with pytest.raises(TargetContractError, match="does not resolve exactly"):
        resolve_commit(project, "f" * 40)
