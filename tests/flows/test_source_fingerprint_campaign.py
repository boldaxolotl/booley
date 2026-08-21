"""Target campaign freshness fingerprints."""

import pytest

from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.fusesoc.fusesoc_registry import UnknownTargetError
from booley.runtime.project_dir import reset_cache


def _write_core(tmp_path, name: str, target: str, source: str) -> None:
    (tmp_path / f"{name}.core").write_text(
        "CAPI=2:\n"
        f"name: ::{name}:0\n"
        f"filesets:\n  rtl: {{files: [{source}]}}\n"
        f"targets:\n  {target}: {{filesets: [rtl], toplevel: dut}}\n",
        encoding="utf-8",
    )


def test_campaign_fingerprint_tracks_tests_and_selected_core(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    _write_core(tmp_path, "design", "sim_unit", "rtl/dut.sv")
    _write_core(tmp_path, "unrelated", "sim_other", "rtl/dut.sv")
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    tests = project_dir / "tests.toml"
    tests.write_text('[sim_unit]\ntests = ["smoke"]\n')

    first = compute_source_fingerprint(tmp_path, target="sim_unit")["campaign"]
    tests.write_text('[sim_unit]\ntests = ["smoke", "regress"]\n')
    tests_changed = compute_source_fingerprint(tmp_path, target="sim_unit")["campaign"]
    assert tests_changed["digest"] != first["digest"]

    (tmp_path / "unrelated.core").write_text(
        (tmp_path / "unrelated.core").read_text() + "# unrelated change\n"
    )
    unrelated_changed = compute_source_fingerprint(tmp_path, target="sim_unit")["campaign"]
    assert unrelated_changed["digest"] == tests_changed["digest"]
    assert unrelated_changed["files"] == [
        ".booley_project/tests.toml",
        "design.core",
    ]


def test_campaign_fingerprint_tracks_transitive_core_changes(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "dependency.core").write_text(
        "CAPI=2:\n"
        "name: ::dependency:0\n"
        "filesets:\n  rtl: {files: [rtl/dut.sv]}\n"
        "targets:\n  default: {filesets: [rtl]}\n",
        encoding="utf-8",
    )
    (tmp_path / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::design:0\n"
        'filesets:\n  rtl: {files: [rtl/dut.sv], depend: ["::dependency:0"]}\n'
        "targets:\n  sim: {filesets: [rtl], toplevel: dut}\n",
        encoding="utf-8",
    )

    first = compute_source_fingerprint(tmp_path, target="sim")["campaign"]
    dependency = tmp_path / "dependency.core"
    dependency.write_text(dependency.read_text() + "# changed option\n")
    changed = compute_source_fingerprint(tmp_path, target="sim")["campaign"]

    assert changed["digest"] != first["digest"]
    assert changed["files"] == ["dependency.core", "design.core"]


def test_campaign_fingerprint_uses_resolved_project_dir(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "rtl").mkdir()
    (checkout / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    _write_core(checkout, "design", "sim", "rtl/dut.sv")
    project_dir = tmp_path / "control"
    project_dir.mkdir()
    tests = project_dir / "tests.toml"
    tests.write_text('[sim]\ntests = ["smoke"]\n')
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
    reset_cache()

    first = compute_source_fingerprint(checkout, target="sim")["campaign"]
    tests.write_text('[sim]\ntests = ["smoke", "corner"]\n')
    changed = compute_source_fingerprint(checkout, target="sim")["campaign"]

    assert changed["digest"] != first["digest"]
    assert changed["files"] == ["design.core", "project-dir/tests.toml"]


def test_unknown_target_fails_instead_of_falling_back(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    _write_core(tmp_path, "design", "sim", "rtl/dut.sv")

    with pytest.raises(UnknownTargetError):
        compute_source_fingerprint(tmp_path, target="missing")
