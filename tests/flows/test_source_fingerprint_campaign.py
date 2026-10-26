"""Target campaign freshness fingerprints."""

import pytest

from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.runtime.booley_dir import reset_cache
from booley.targets.domain import UnknownTargetError
from tests.conftest import symlink_or_skip


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
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    tests = booley_dir / "tests.toml"
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
        ".booley/tests.toml",
        "design.core",
    ]


def test_campaign_fingerprint_names_files_through_symlinked_booley_dir(tmp_path) -> None:
    root = tmp_path / "project"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    data = tmp_path / "booley-data"
    (data / "cores").mkdir(parents=True)
    (data / "tests.toml").write_text('[sim_unit]\ntests = ["smoke"]\n')
    symlink_or_skip(root / ".booley", data)
    reset_cache()
    # A stealth core lives in the Booley Directory, whose target is outside the root.
    (data / "cores" / "design.core").write_text(
        "CAPI=2:\nname: ::design:0\n"
        "filesets:\n  rtl: {files: [../../project/rtl/dut.sv]}\n"
        "targets:\n  sim_unit: {filesets: [rtl], toplevel: dut}\n",
        encoding="utf-8",
    )

    campaign = compute_source_fingerprint(root, target="sim_unit")["campaign"]

    assert campaign["files"] == [".booley/cores/design.core", ".booley/tests.toml"]


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


def test_campaign_fingerprint_uses_resolved_booley_dir(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "rtl").mkdir()
    (checkout / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    _write_core(checkout, "design", "sim", "rtl/dut.sv")
    booley_dir = tmp_path / "control"
    booley_dir.mkdir()
    tests = booley_dir / "tests.toml"
    tests.write_text('[sim]\ntests = ["smoke"]\n')
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
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


def test_workload_fingerprint_tracks_pre_sim_program_not_its_arguments(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    _write_core(tmp_path, "design", "sim", "rtl/dut.sv")
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n')
    (booley_dir / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["python3 hooks/build.py --output generated/report.py"]\n'
    )
    script = tmp_path / "hooks" / "build.py"
    script.parent.mkdir()
    script.write_text("print('build')\n")
    output = tmp_path / "generated" / "report.py"
    output.parent.mkdir()
    output.write_text("generated\n")
    reset_cache()

    workload = compute_source_fingerprint(tmp_path, target="sim")["workload"]

    assert workload["files"] == ["hooks/build.py", "rtl/dut.sv"]


# Independently captured from origin/main 9da903961 using its original source,
# surface, Goal resolver and selected-input adapter on the fixture below.
_GOLDEN_1337 = '{"selected":{"algorithm":"sha256","campaign":{"digest":"eb48344a8e37d572036aef167d9d8279a21edf6245cb1aae0717b702cdced67b","files":[".booley/tests.toml","design.core"]},"rtl":{"digest":"23a11f8393229e2945923aab32a010401bd5c5c4a5d9b6b40884e2ec6d8d5036","files":["data.hex","dut.v"]},"rtl_dirs":["."],"target":"top","tb":{"digest":"6e69edf2b14b48e26a0cac94366c5217b0ce1f49952a2cd6f7391a271fdcaada","files":["tb.py"]},"tb_dirs":["."],"work_dir":"<ROOT>","workload":{"digest":"340bdff048c1c377f96e39b100ea6ceec28610e0fa5ab6012502bc278ccc7216","files":["build.py","data.hex","dut.v","tb.py"]}},"selected_goal":{"algorithm":"sha256","campaign":{"digest":"eb48344a8e37d572036aef167d9d8279a21edf6245cb1aae0717b702cdced67b","files":[".booley/tests.toml","design.core"]},"rtl":{"digest":"23a11f8393229e2945923aab32a010401bd5c5c4a5d9b6b40884e2ec6d8d5036","files":["data.hex","dut.v"]},"rtl_dirs":["."],"target":"top","target_surface":{"digest":"e593942fe084c38f2b8dbf1430681a2bd8b08679e8d575c25b6283e2e7267c58","files":["design.core","data.hex","tb.py","build.py",".booley/tests.toml"]},"tb":{"digest":"6e69edf2b14b48e26a0cac94366c5217b0ce1f49952a2cd6f7391a271fdcaada","files":["tb.py"]},"tb_dirs":["."],"work_dir":"<ROOT>","workload":{"digest":"340bdff048c1c377f96e39b100ea6ceec28610e0fa5ab6012502bc278ccc7216","files":["build.py","data.hex","dut.v","tb.py"]}},"selected_protection":["build.py","dut.v"],"shared":{"algorithm":"sha256","campaign":{"digest":"eb48344a8e37d572036aef167d9d8279a21edf6245cb1aae0717b702cdced67b","files":[".booley/tests.toml","design.core"]},"rtl":{"digest":"23a11f8393229e2945923aab32a010401bd5c5c4a5d9b6b40884e2ec6d8d5036","files":["data.hex","dut.v"]},"rtl_dirs":["."],"target":null,"tb":{"digest":"6e69edf2b14b48e26a0cac94366c5217b0ce1f49952a2cd6f7391a271fdcaada","files":["tb.py"]},"tb_dirs":["."],"work_dir":"<ROOT>","workload":{"digest":"340bdff048c1c377f96e39b100ea6ceec28610e0fa5ab6012502bc278ccc7216","files":["build.py","data.hex","dut.v","tb.py"]}},"surface":{"digest":"8dc44f8b6f15eaede9ebc946a04c3010f5ffc54ad52b12f7a94e3df6236c7ef3","files":["design.core","data.hex","tb.py","build.py",".booley/tests.toml"]}}'


def _compatibility_fixture(tmp_path):
    from tests.goals.conftest import git

    git(tmp_path, "init", "-q")
    # The golden digests cover these bytes, so no platform newline translation.
    (tmp_path / ".git/info/exclude").write_text("data.hex\n")
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley/booley.toml").write_bytes(
        b'[flows.sim]\npre_run_commands = ["python3 build.py"]\n'
    )
    (tmp_path / ".booley/tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    (tmp_path / "design.core").write_bytes(
        b"CAPI=2:\nname: ::design:0\nfilesets:\n"
        b"  rtl: {files: [dut.v, {data.hex: {file_type: user}}], file_type: verilogSource}\n"
        b"  tb: {files: [tb.py], file_type: user, tags: [tb]}\n"
        b"targets:\n  top: {filesets: [rtl, tb], toplevel: dut}\n"
        b"scripts:\n  prepare: {cmd: [python3, build.py]}\n"
    )
    (tmp_path / "dut.v").write_bytes(b"module dut; endmodule\n")
    (tmp_path / "data.hex").write_bytes(b"0011\n")
    (tmp_path / "tb.py").write_bytes(b"print('testbench')\n")
    (tmp_path / "build.py").write_bytes(b"print('build')\n")
    reset_cache()


def test_shared_and_selected_fingerprints_match_current_layout_golden(tmp_path):
    import json

    from booley.goals.freshness import DEFAULT_RESOLVERS
    from booley.goals.target_surface import target_surface_fingerprint
    from booley.mcp.goal_generated_inputs import _committed_only_inputs
    from booley.targets.catalog import TargetCatalog

    _compatibility_fixture(tmp_path)
    rows = {}
    for label, target in (("shared", None), ("selected", "top")):
        value = compute_source_fingerprint(tmp_path, target=target)
        value["work_dir"] = "<ROOT>"
        rows[label] = value
    rows["selected_goal"] = DEFAULT_RESOLVERS.fingerprint(tmp_path, target="top")
    rows["selected_goal"]["work_dir"] = "<ROOT>"
    rows["surface"] = target_surface_fingerprint(tmp_path, None)
    rows["selected_protection"] = sorted(
        str(path.relative_to(tmp_path))
        for path in _committed_only_inputs(tmp_path, TargetCatalog.build(tmp_path), "top")
    )
    assert rows == json.loads(_GOLDEN_1337)
    assert json.dumps(rows, sort_keys=True, separators=(",", ":")) == _GOLDEN_1337


@pytest.mark.parametrize("target", [None, "sim"])
def test_sim_harness_fingerprint_preserves_all_selected_inputs(tmp_path, target):
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)
    fingerprint = compute_source_fingerprint(tmp_path, target=target)
    assert fingerprint["rtl"]["files"] == ["shared/dut.sv"]
    assert {"shared/main.cpp", "shared/driver.c", "shared/api.h", "shared/tb.sv"} <= set(
        fingerprint["tb"]["files"]
    )
    assert ("shared/unused.cpp" in fingerprint["tb"]["files"]) == (target is None)
    before = fingerprint["tb"]["digest"]
    (tmp_path / "shared/main.cpp").write_text("// changed harness\n")
    assert compute_source_fingerprint(tmp_path, target=target)["tb"]["digest"] != before
