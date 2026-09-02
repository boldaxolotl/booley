"""Focused regression tests for design-size domain analysis."""

import ast
from pathlib import Path

from tests.architecture.production import assert_no_dependencies

from booley.audit import design_size

_ROOT = Path(__file__).resolve().parents[2]


def _write_core(root: Path, source: str = "rtl/selected.sv") -> None:
    (root / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::small:0\n"
        "filesets:\n"
        "  rtl:\n"
        f"    files: [{source}]\n"
        "targets:\n"
        "  sim_small:\n"
        "    filesets: [rtl]\n"
        "    toplevel: selected\n",
        encoding="utf-8",
    )


def test_configured_target_closure_takes_precedence_over_repository_size(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "selected.sv").write_text(
        "module selected;\nendmodule\n", encoding="utf-8"
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    for index in range(design_size.LARGE_DESIGN_FILES + 1):
        (unrelated / f"large_{index}.sv").write_text("module large; endmodule\n")
    _write_core(tmp_path)

    audit = design_size.analyze_design_size(tmp_path, tmp_path / ".state", ["sim_small"])

    assert audit == design_size.DesignSizeAudit(
        hdl_files=1,
        lines_of_code=2,
        scope=design_size.DesignSizeScope.CONFIGURED_TARGETS,
    )
    assert not audit.exceeds_deep_smoke_budget


def test_repository_scan_prunes_generated_and_internal_directories(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "core.v").write_text("module core; endmodule\n", encoding="utf-8")
    generated = tmp_path / "build"
    generated.mkdir()
    (generated / "ignored.sv").write_text("module ignored; endmodule\n", encoding="utf-8")
    project_dir = tmp_path / ".custom" / "project-data"
    project_dir.mkdir(parents=True)
    (project_dir / "internal.sv").write_text("module internal; endmodule\n", encoding="utf-8")

    audit = design_size.analyze_design_size(tmp_path, project_dir, [])

    assert audit.hdl_files == 1
    assert audit.lines_of_code == 1
    assert audit.scope is design_size.DesignSizeScope.REPOSITORY


def test_unresolvable_target_falls_back_to_repository_scan(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "core.svh").write_text("`define WIDTH 8\n", encoding="utf-8")

    audit = design_size.analyze_design_size(
        tmp_path,
        tmp_path / ".state",
        ["missing_target"],
    )

    assert audit.hdl_files == 1
    assert audit.scope is design_size.DesignSizeScope.REPOSITORY


def test_deep_smoke_budget_boundary_is_inclusive() -> None:
    file_boundary = design_size.DesignSizeAudit(
        hdl_files=design_size.LARGE_DESIGN_FILES,
        lines_of_code=0,
        scope=design_size.DesignSizeScope.REPOSITORY,
    )
    loc_boundary = design_size.DesignSizeAudit(
        hdl_files=0,
        lines_of_code=design_size.LARGE_DESIGN_LOC,
        scope=design_size.DesignSizeScope.REPOSITORY,
    )

    assert file_boundary.exceeds_deep_smoke_budget
    assert loc_boundary.exceeds_deep_smoke_budget


def test_doctor_does_not_reimplement_extracted_mechanisms() -> None:
    doctor_path = _ROOT / "src" / "booley" / "harness" / "doctor.py"
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert not function_names & {"_count_design_files", "_configured_design_size", "_design_size"}


def test_design_size_domain_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "design_size.py"
    assert_no_dependencies(
        paths=(module_path,),
        target_prefixes=("booley.harness", "booley.mcp", "booley.specialists"),
    )
