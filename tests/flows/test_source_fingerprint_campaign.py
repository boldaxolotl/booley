"""Target campaign freshness fingerprints."""

from booley.flows.source_fingerprint import compute_source_fingerprint


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
