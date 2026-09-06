"""Regression tests for structured simulation Target validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

from booley.ticket_board.validation import validate_ticket_fields

_TOY_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: ::toy:0

    filesets:
      rtl:
        files: [rtl/toy_top.sv]
        file_type: systemVerilogSource
      tb:
        files: [tb/toy_tb.sv]
        file_type: systemVerilogSource

    targets:
      toy_compile:
        flow: lint
        flow_options: {tool: verilator}
        filesets: [rtl]
        toplevel: toy_top
      sim_toy:
        flow: sim
        flow_options: {tool: verilator}
        filesets: [rtl, tb]
        toplevel: toy_tb
    """
)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "toy.core").write_text(_TOY_CORE, encoding="utf-8")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "toy_top.sv").write_text(
        "module toy_top(input logic a_i, output logic y_o); assign y_o = a_i; endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "toy_tb.sv").write_text(
        "module toy_tb; endmodule\n",
        encoding="utf-8",
    )
    return tmp_path


def _fields(target: str, *, tb_path: str = "tb/toy_tb.sv") -> dict:
    return {
        "summary": "Validate a simulation selector",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/toy_top.sv", "tb/toy_tb.sv"],
        "criteria": {"mandatory": {"sim_pass": [f"{tb_path} @ {target} @ all @ pass -> pass"]}},
    }


def test_lint_target_cannot_satisfy_structured_sim_pass(tmp_path: Path) -> None:
    project = _project(tmp_path)

    errors = validate_ticket_fields(
        _fields("toy_compile"),
        "## Description\nExercise target validation.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any(
        "toy_compile" in error and "cannot satisfy sim_pass" in error and "sim_toy" in error
        for error in errors
    )


def test_sim_target_satisfies_structured_sim_pass(tmp_path: Path) -> None:
    project = _project(tmp_path)

    errors = validate_ticket_fields(
        _fields("sim_toy"),
        "## Description\nExercise target validation.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert errors == []


def test_icarus_sim_target_satisfies_structured_sim_pass(tmp_path: Path) -> None:
    project = _project(tmp_path)
    core = project / "toy.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace("tool: verilator", "tool: icarus"),
        encoding="utf-8",
    )

    errors = validate_ticket_fields(
        _fields("sim_toy"),
        "## Description\nExercise target validation.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert errors == []


def test_unknown_sim_target_names_eligible_correction(tmp_path: Path) -> None:
    project = _project(tmp_path)

    errors = validate_ticket_fields(
        _fields("missing"),
        "## Description\nExercise target validation.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any(
        "Unknown target 'missing'" in error and "eligible simulation Targets: sim_toy" in error
        for error in errors
    )


def test_recorded_sim_target_is_not_resolved_in_destination_view(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("contract_only")
    fields["acceptance_basis"] = {
        "schema": 1,
        "participants": [
            {
                "role": "outer",
                "authoring_sha": "a" * 40,
                "ticket_ref": "refs/heads/booley-generation/1234567890abcdef/ticket",
                "destination_ref": "refs/heads/main",
                "destination_sha": "b" * 40,
            }
        ],
    }

    errors = validate_ticket_fields(
        fields,
        "## Description\nExercise recorded target validation.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert not any("contract_only" in error for error in errors)


def test_ticket_created_sim_target_is_deferred(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("sim_future")
    fields["scope"].append("toy.core")

    errors = validate_ticket_fields(
        fields,
        "## Description\nAdd the `sim_future` Target to toy.core.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert errors == []


def test_future_sim_target_requires_scoped_core(tmp_path: Path) -> None:
    project = _project(tmp_path)

    errors = validate_ticket_fields(
        _fields("sim_future"),
        "## Description\nAdd the `sim_future` Target.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any("Unknown target 'sim_future'" in error for error in errors)


def test_future_sim_target_requires_explicit_creation_text(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("sim_future")
    fields["scope"].append("toy.core")

    errors = validate_ticket_fields(
        fields,
        "## Description\nRun the `sim_future` Target.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any("Unknown target 'sim_future'" in error for error in errors)


def test_future_sim_target_name_must_be_explicit(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("sim")
    fields["scope"].append("toy.core")

    errors = validate_ticket_fields(
        fields,
        "## Description\nAdd simulation support to toy.core.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any("Unknown target 'sim'" in error for error in errors)


def test_existing_non_sim_target_is_not_deferred(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("toy_compile")
    fields["scope"].append("toy.core")

    errors = validate_ticket_fields(
        fields,
        "## Description\nExtend toy.core with the `toy_compile` Target.",
        check_files=True,
        check_tb_files=False,
        project_root=project,
    )

    assert any("cannot satisfy sim_pass" in error for error in errors)


def test_future_target_allows_scoped_new_root_testbench(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("sim_future", tb_path="future_tb.sv")
    fields["scope"] = ["rtl/toy_top.sv", "future_tb.sv [new]", "toy.core"]

    errors = validate_ticket_fields(
        fields,
        "## Description\nCreate the `sim_future` Target in toy.core.",
        check_files=True,
        project_root=project,
    )

    assert errors == []


def test_future_target_does_not_allow_unscoped_new_testbench(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fields = _fields("sim_future", tb_path="future_tb.sv")
    fields["scope"] = ["rtl/toy_top.sv", "toy.core"]

    errors = validate_ticket_fields(
        fields,
        "## Description\nCreate the `sim_future` Target in toy.core.",
        check_files=True,
        project_root=project,
    )

    assert any("testbench source_dir" in error for error in errors)
