"""Immutable Target contract schema, digest, and criterion validation."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter
from booley.ticket_board.target_contract import (
    TargetContract,
    TargetContractError,
    build_contract,
    criterion_targets,
    resolve_commit,
    surface_digest,
    validate_contract_fields,
    validate_criterion_targets,
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
    (tmp_path / "tb" / "toy_tb.sv").write_text(
        "module toy_tb; endmodule\n", encoding="utf-8"
    )
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
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_contract_round_trips_as_nested_frontmatter(tmp_path: Path) -> None:
    project = _project(tmp_path)
    contract = build_contract(
        project,
        outer_sha="a" * 40,
        targets=["sim_toy", "lint_toy"],
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


def test_contract_rejects_caller_fabricated_base_sha(tmp_path: Path) -> None:
    project = _project(tmp_path)
    contract = build_contract(project, outer_sha="a" * 40, targets=["sim_toy"])

    errors = validate_contract_fields(
        {"base_sha": "b" * 40, "target_contract": contract.as_dict()}
    )

    assert errors == ["base_sha must equal target_contract.outer_sha"]


def test_contract_requires_sorted_unique_targets() -> None:
    with pytest.raises(TargetContractError, match="sorted and unique"):
        TargetContract.from_mapping(
            {
                "schema": 1,
                "outer_sha": "a" * 40,
                "project_sha": "",
                "surface_digest": "b" * 64,
                "targets": ["sim_b", "sim_a", "sim_a"],
            }
        )


def test_surface_ignores_rtl_but_covers_every_control_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = surface_digest(project)
    (project / "rtl" / "toy.sv").write_text("module toy; wire changed; endmodule\n")
    assert surface_digest(project) == original

    core = project / "toy.core"
    core.write_text(core.read_text(encoding="utf-8").replace("toplevel: toy\n", "toplevel: toy_v2\n"))
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


def test_non_target_booley_config_does_not_change_surface(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".booley_project" / "booley.toml"
    original = surface_digest(project)

    config.write_text(config.read_text(encoding="utf-8").replace("true", "false"))

    assert surface_digest(project) == original


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
    config.write_text(
        "[flows.sim]\ndefault_target = 'sim_toy'\npre_run = 'hooks/select'\n"
    )
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

    fields["scope"] = ["rtl/future.sv"]
    errors = validate_criterion_targets(fields, project)
    assert any("not declared Scope [new]" in error for error in errors)


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
