"""Tests for ``booley init --scaffold`` in the setup.scaffold module.

The generators are pure (choices in, files out), so most combos are validated
without touching disk; the step-level tests exercise the refusal gate and the
actual writes in a tmp repo.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from booley.fusesoc import selftest_overlay
from booley.fusesoc.fusesoc_registry import (
    _resolve_target,
    core_schema_errors,
    read_core,
)
from booley.harness import doctor
from booley.harness.init_cmd import BOOLEY_TOML_SKELETON, TESTS_TOML_SKELETON
from booley.harness.setup.common import InitContext
from booley.harness.setup.scaffold import (
    ScaffoldChoices,
    existing_design_files,
    gather_scaffold_choices,
    scaffold_files,
    step_scaffold,
)
from booley.runtime.booley_dir import reset_cache
from booley.targets.catalog import TargetCatalog
from booley.targets.target_surface import collect_surface
from tests.conftest import symlink_or_skip


def _choices(**overrides) -> ScaffoldChoices:
    defaults = {
        "name": "my_ip",
        "sim_eda_tool": "verilator",
        "tb_style": "sv",
        "lint_eda_tool": "verilator",
        "asic": True,
        "fpga_part": None,
    }
    defaults.update(overrides)
    return ScaffoldChoices(**defaults)


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "scaffold": "my_ip",
        "sim_eda_tool": None,
        "tb_style": None,
        "lint_eda_tool": None,
        "asic": None,
        "fpga_part": None,
        "stealth": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _ctx(project_root: Path, monkeypatch, **kw) -> InitContext:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    return InitContext(project_root=project_root, interactive=False, **kw)


def _core(files: dict[str, str]) -> dict:
    return yaml.safe_load(files["my_ip.core"])


def _selftest_core(files: dict[str, str]) -> dict:
    return yaml.safe_load(files["verif/booley_doctor_selftest.core"])


# ---------------------------------------------------------------------------
# Generators — per-combo shapes
# ---------------------------------------------------------------------------


def test_default_combo_files_and_shapes() -> None:
    files = scaffold_files(_choices())

    assert set(files) == {
        "rtl/my_ip.sv",
        "tb/tb_my_ip.sv",
        "tb/sim_main.cpp",
        "my_ip.core",
        "verif/booley_doctor_selftest.core",
        "verif/booley_doctor_selftest/lint_bad.sv",
        "constraints/my_ip.sdc",
        ".booley/booley.toml",
        ".booley/tests.toml",
        ".booley/selftest/sim/bad-overlay/my_ip.sv",
    }

    core = _core(files)
    assert core["name"] == "::my_ip:0"
    sim = core["targets"]["sim"]
    assert sim["flow"] == "sim"
    assert sim["flow_options"]["tool"] == "verilator"
    # The owned C++ main drives the event scheduler and trace lifecycle.
    assert sim["flow_options"]["verilator_options"] == ["--timing", "--exe"]
    assert sim["toplevel"] == "tb_my_ip"
    assert core["filesets"]["tb"]["tags"] == ["tb"]

    lint = core["targets"]["lint"]
    assert lint["flow"] == "lint"
    assert lint["flow_options"] == {
        "tool": "verilator",
        "booley": {"doctor": ["lint"]},
    }
    assert lint["toplevel"] == "my_ip"

    synth = core["targets"]["synth"]
    assert synth["flow"] == "generic"
    assert synth["flow_options"] == {
        "tool": "yosys",
        "arch": "xilinx",
        "synth_mode": "physical",
        "booley": {"doctor": ["synth"]},
    }
    assert "constraints" in synth["filesets"]
    sdc_entry = core["filesets"]["constraints"]["files"][0]
    assert sdc_entry == {"constraints/my_ip.sdc": {"file_type": "SDC"}}

    cfg = tomllib.loads(files[".booley/booley.toml"])
    assert cfg["project"]["name"] == "my_ip"
    # Stealth is OFF in a scaffolded repo: it exists to exercise Booley, so
    # the commit sanitizer would redact its own MCP tool name from subjects and
    # drop multi-line bodies for no privacy gain.
    assert cfg["stealth"] == {"enabled": False}
    # Target selection lives beside each Target in the .core file.
    assert cfg["flows"]["sim"] == {"trace_files": ["dump.vcd"]}
    assert cfg["flows"]["lint"] == {}
    assert "elab" not in cfg["flows"]
    assert cfg["flows"]["synth"] == {}
    assert sim["flow_options"]["booley"]["doctor"] == ["sim"]
    assert cfg["flows"]["fpga"] == {"enabled": False}

    tests_cfg = tomllib.loads(files[".booley/tests.toml"])
    assert tests_cfg["sim"]["tests"] == ["reset", "count"]
    assert tests_cfg["sim"]["select"] == "+test_id={index}"

    tb = files["tb/tb_my_ip.sv"]
    assert "[SIM_RESULT] PASSED" in tb and "[SIM_RESULT] FAILED" in tb
    assert ") dut (" in tb  # instance MUST be named dut (tb_style_guide §12)
    assert "$value$plusargs" in tb

    # RTL and TB must BOTH declare a timescale: Verilator 5 --timing promotes
    # a mixed-presence timescale (TIMESCALEMOD) to an elaboration error, which
    # broke the pristine scaffold's own doctor --deep sim smoke.
    rtl = files["rtl/my_ip.sv"]
    assert "`timescale 1ns / 1ps" in rtl
    assert "`timescale 1ns / 1ps" in tb


def test_sim_bad_overlay_only_injects_the_reset_defect() -> None:
    files = scaffold_files(_choices())
    good_rtl = files["rtl/my_ip.sv"]
    bad_rtl = files[".booley/selftest/sim/bad-overlay/my_ip.sv"]
    good_body = good_rtl[good_rtl.index("`timescale") :]
    bad_body = bad_rtl[bad_rtl.index("`timescale") :]

    assert bad_body == good_body.replace(
        "count <= '0;", "count <= '1;  // Deliberate Doctor self-test defect."
    )


@pytest.mark.parametrize("lint_eda_tool", ["verilator", "verible"])
def test_scaffold_supplies_doctor_fail_path_fixtures(
    tmp_path: Path, lint_eda_tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = scaffold_files(_choices(lint_eda_tool=lint_eda_tool))

    sim_overlay = ".booley/selftest/sim/bad-overlay/my_ip.sv"
    lint_bad_source = "verif/booley_doctor_selftest/lint_bad.sv"
    lint_bad_core = "verif/booley_doctor_selftest.core"
    assert sim_overlay in files
    assert lint_bad_source in files
    assert lint_bad_core in files

    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    booley_dir = tmp_path / ".booley"
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=booley_dir,
        booley_toml=tomllib.loads((booley_dir / "booley.toml").read_text(encoding="utf-8")),
        configs_toml=tomllib.loads((booley_dir / "tests.toml").read_text(encoding="utf-8")),
        first_target="sim",
    )
    warnings: list[str] = []

    sim_plan = doctor._selftest_plan(project, "sim", warnings.append)
    lint_plan = doctor._selftest_plan(project, "lint", warnings.append)

    assert warnings == []
    assert sim_plan is not None
    assert sim_plan.good.target == sim_plan.bad.target == "sim"
    assert lint_plan is not None
    assert lint_plan.good.target == "lint"
    assert lint_plan.bad.target == "lint_selftest_bad"

    core = _core(files)
    assert "lint_selftest_bad" not in core["targets"]
    assert "lint_selftest_bad" not in core["filesets"]

    selftest_core = _selftest_core(files)
    lint_bad = selftest_core["targets"]["lint_selftest_bad"]
    assert lint_bad["flow"] == "lint"
    assert lint_bad["flow_options"] == {
        "tool": lint_eda_tool,
        "booley": {"doctor_selftest": True},
    }
    assert lint_bad["filesets"] == ["lint_selftest_bad"]
    assert lint_bad["toplevel"] == "my_ip_lint_selftest_bad"
    assert selftest_core["filesets"]["lint_selftest_bad"]["files"] == [
        {"booley_doctor_selftest/lint_bad.sv": {"file_type": "systemVerilogSource"}}
    ]

    assert core_schema_errors(tmp_path / lint_bad_core) == []
    public = {handle.name for handle in TargetCatalog.build(tmp_path).list()}
    assert "lint_selftest_bad" not in {entry.name for entry in collect_surface(tmp_path).entries()}
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    doctor_targets = {handle.name for handle in TargetCatalog.build(tmp_path).list()}
    assert "lint_selftest_bad" in doctor_targets
    assert "lint_selftest_bad" not in public


def test_scaffold_sim_bad_overlay_replaces_the_staged_rtl(tmp_path: Path) -> None:
    pytest.importorskip("fusesoc")
    files = scaffold_files(_choices())
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    resolved = _resolve_target("sim", project_root=tmp_path, build_root=tmp_path / "build")
    staged_rtl = resolved.build_root / "my_ip.sv"
    assert staged_rtl.read_text(encoding="utf-8") == files["rtl/my_ip.sv"]

    copied = selftest_overlay.stage_bad_overlay(tmp_path / ".booley", "sim", resolved.build_root)

    assert copied == 1
    assert (
        staged_rtl.read_text(encoding="utf-8")
        == files[".booley/selftest/sim/bad-overlay/my_ip.sv"]
    )


def test_scaffold_lint_bad_target_resolves_from_dedicated_core(tmp_path: Path) -> None:
    pytest.importorskip("fusesoc")
    files = scaffold_files(_choices())
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    resolved = _resolve_target(
        "lint_selftest_bad", project_root=tmp_path, build_root=tmp_path / "build"
    )

    assert resolved.vlnv == "::my_ip_booley_doctor_selftest:0"
    assert resolved.eda_tool == "verilator"
    assert len(resolved.rtl_source_files) == 1
    source = resolved.rtl_source_files[0]
    normalized_source_name = source.name.replace("\\", "/")
    assert normalized_source_name.endswith("/booley_doctor_selftest/lint_bad.sv")
    assert source.absolute(resolved.build_root).is_file()


def test_asic_scaffold_sdc_excludes_clock_and_asynchronous_reset_from_data_timing() -> None:
    sdc = scaffold_files(_choices())["constraints/my_ip.sdc"]

    assert "set_input_delay  -clock clk 0.0 [get_ports en]" in sdc
    assert "set_output_delay -clock clk 0.0 [get_ports count]" in sdc
    assert "set_false_path -from [get_ports rst_n]" in sdc
    assert "[all_inputs]" not in sdc
    assert "[all_outputs]" not in sdc


def test_icarus_combo_uses_g2012() -> None:
    core = _core(scaffold_files(_choices(sim_eda_tool="icarus")))
    opts = core["targets"]["sim"]["flow_options"]
    assert opts["tool"] == "icarus"
    assert opts["iverilog_options"] == ["-g2012"]
    assert "verilator_options" not in opts


def test_icarus_lint_target_gets_g2012() -> None:
    # The scaffold's RTL is SystemVerilog, and iverilog defaults to
    # Verilog-2005 — a NON-sim icarus target (lint/elaborate) used to be
    # exactly where -g2012 got forgotten, manufacturing syntax errors that
    # read like design bugs. LINT_EDA_TOOLS has no Icarus entry today; the rule is
    # keyed on the EDA tool so the gap cannot reopen if the wizard grows one.
    core = _core(scaffold_files(_choices(lint_eda_tool="icarus")))
    opts = core["targets"]["lint"]["flow_options"]
    assert opts == {
        "tool": "icarus",
        "iverilog_options": ["-g2012"],
        "booley": {"doctor": ["lint"]},
    }


def test_cocotb_combo_shapes() -> None:
    files = scaffold_files(_choices(sim_eda_tool="icarus", tb_style="cocotb"))
    assert "tb/test_my_ip.py" in files
    assert "tb/tb_my_ip.sv" not in files

    core = _core(files)
    sim = core["targets"]["sim"]
    assert sim["flow_options"]["cocotb_module"] == "test_my_ip"
    assert sim["flow_options"]["iverilog_options"] == ["-g2012"]
    assert sim["toplevel"] == "my_ip"  # the DUT itself — no HDL wrapper
    tb_entry = core["filesets"]["tb"]["files"][0]
    assert tb_entry == {"tb/test_my_ip.py": {"file_type": "user", "copyto": "test_my_ip.py"}}

    tests_cfg = tomllib.loads(files[".booley/tests.toml"])
    assert tests_cfg["sim"]["tests"] == ["test_reset", "test_count"]
    # A select plusarg on a Cocotb Target is a setup-time error — never emit one.
    assert "select" not in tests_cfg["sim"]

    tb = files["tb/test_my_ip.py"]
    assert "@cocotb.test()" in tb
    assert "[SIM_RESULT]" not in tb  # cocotb verdicts come from results.xml


def test_cocotb_verilator_options() -> None:
    core = _core(scaffold_files(_choices(sim_eda_tool="verilator", tb_style="cocotb")))
    opts = core["targets"]["sim"]["flow_options"]
    assert opts["verilator_options"] == ["--timing", "-Wno-fatal"]


def test_verible_lint_keeps_elaboration_check_on_sim() -> None:
    files = scaffold_files(_choices(lint_eda_tool="verible"))
    core = _core(files)
    assert core["targets"]["lint"]["flow_options"] == {
        "tool": "verible",
        "booley": {"doctor": ["lint"]},
    }

    cfg = tomllib.loads(files[".booley/booley.toml"])
    assert "elab" not in cfg["flows"]
    assert core["targets"]["sim"]["flow_options"]["booley"]["doctor"] == ["sim"]


def test_verible_lint_has_no_separate_elaboration_flow() -> None:
    files = scaffold_files(_choices(lint_eda_tool="verible"))
    cfg = tomllib.loads(files[".booley/booley.toml"])
    assert "elab" not in cfg["flows"]


def test_no_asic_omits_synth_target_and_sdc() -> None:
    files = scaffold_files(_choices(asic=False))
    assert "constraints/my_ip.sdc" not in files
    core = _core(files)
    assert "synth" not in core["targets"]
    assert "constraints" not in core["filesets"]
    cfg = tomllib.loads(files[".booley/booley.toml"])
    assert cfg["flows"]["synth"]["enabled"] is False
    assert "default_target" not in cfg["flows"]["synth"]


def test_fpga_part_emits_fpga_target_and_xdc() -> None:
    files = scaffold_files(_choices(fpga_part="xc7a200tfbg484-1"))
    assert "constraints/my_ip.xdc" in files
    core = _core(files)
    fpga = core["targets"]["fpga"]
    assert fpga["flow"] == "generic"
    assert fpga["flow_options"] == {
        "tool": "vivado",
        "part": "xc7a200tfbg484-1",
        "ppa_profile": "balanced",
        "out_of_context": True,
        "booley": {"doctor": ["fpga"]},
    }
    assert "xdc" in fpga["filesets"]
    xdc_entry = core["filesets"]["xdc"]["files"][0]
    assert xdc_entry == {"constraints/my_ip.xdc": {"file_type": "xdc"}}

    cfg = tomllib.loads(files[".booley/booley.toml"])
    assert "backend" not in cfg["flows"]["fpga"]
    assert cfg["flows"]["fpga"] == {}


@pytest.mark.parametrize("tb_style", ["sv", "cocotb"])
@pytest.mark.parametrize("sim_eda_tool", ["verilator", "icarus"])
def test_every_combo_parses_with_booleys_own_readers(
    tmp_path: Path, monkeypatch, sim_eda_tool: str, tb_style: str
) -> None:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    files = scaffold_files(
        _choices(sim_eda_tool=sim_eda_tool, tb_style=tb_style, fpga_part="xc7a35tcpg236-1")
    )
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    core = read_core(tmp_path / "my_ip.core")  # raises on unparseable YAML
    assert core["CAPI=2"] is None  # marker line parsed as expected

    targets = {handle.name for handle in TargetCatalog.build(tmp_path).list()}
    expected = {"sim", "lint", "synth", "fpga"}
    assert expected <= targets
    assert "lint_selftest_bad" not in targets
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    doctor_targets = {handle.name for handle in TargetCatalog.build(tmp_path).list()}
    assert "lint_selftest_bad" in doctor_targets
    reset_cache()


# ---------------------------------------------------------------------------
# Wizard answer resolution
# ---------------------------------------------------------------------------


def test_gather_rejects_invalid_name(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    for bad in ("My_IP", "9lives", "has-dash", ""):
        assert gather_scaffold_choices(_args(scaffold=bad), ctx) is None
    capsys.readouterr()


@pytest.mark.parametrize("unsupported", ["xcelium", "vcs"])
def test_gather_rejects_unsupported_commercial_simulators(
    tmp_path: Path, monkeypatch, capsys, unsupported: str
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    args = _args(sim_eda_tool=unsupported)
    assert gather_scaffold_choices(args, ctx) is None
    assert "future roadmap" in capsys.readouterr().out


def test_gather_non_interactive_defaults(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    choices = gather_scaffold_choices(_args(), ctx)
    assert choices == ScaffoldChoices(
        name="my_ip",
        sim_eda_tool="verilator",
        tb_style="sv",
        lint_eda_tool="verilator",
        asic=True,
        fpga_part=None,
    )
    capsys.readouterr()


def test_gather_flags_win_over_defaults(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    args = _args(sim_eda_tool="icarus", tb_style="cocotb", lint_eda_tool="verible", asic=False)
    choices = gather_scaffold_choices(args, ctx)
    assert choices is not None
    assert (choices.sim_eda_tool, choices.tb_style, choices.lint_eda_tool, choices.asic) == (
        "icarus",
        "cocotb",
        "verible",
        False,
    )
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Refusal gate + the step itself
# ---------------------------------------------------------------------------


def test_existing_design_files_finds_rtl_and_cores(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "old.v").write_text("module old; endmodule\n")
    (tmp_path / "chip.core").write_text("CAPI=2:\n")
    # Hidden trees are skipped...
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "junk.sv").write_text("not rtl\n")
    # ...except stealth authored cores (ADR 0036).
    stealth = tmp_path / ".booley" / "cores"
    stealth.mkdir(parents=True)
    (stealth / "stealth.core").write_text("CAPI=2:\n")

    found = {p.as_posix() for p in existing_design_files(tmp_path)}
    assert found == {"rtl/old.v", "chip.core", ".booley/cores/stealth.core"}


def test_step_refuses_on_existing_design(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    assert not (tmp_path / "rtl").exists()
    capsys.readouterr()


def test_step_refuses_on_populated_config(tmp_path: Path, monkeypatch, capsys) -> None:
    pd = tmp_path / ".booley"
    pd.mkdir()
    (pd / "booley.toml").write_text('[project]\nname = "existing"\n')
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    capsys.readouterr()


def test_step_overwrites_comment_only_skeletons(tmp_path: Path, monkeypatch, capsys) -> None:
    # A prior plain `booley init` leaves comment-only skeletons; scaffolding
    # over them is the supported "ran init first, then decided to scaffold" path.
    pd = tmp_path / ".booley"
    pd.mkdir()
    (pd / "booley.toml").write_text(BOOLEY_TOML_SKELETON, encoding="utf-8")
    (pd / "tests.toml").write_text(TESTS_TOML_SKELETON, encoding="utf-8")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is True
    cfg = tomllib.loads((pd / "booley.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["name"] == "my_ip"
    capsys.readouterr()


def test_step_writes_everything_in_fresh_repo(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is True
    assert ctx.results[-1].status == "ok"
    for rel in (
        "rtl/my_ip.sv",
        "tb/tb_my_ip.sv",
        "my_ip.core",
        "verif/booley_doctor_selftest.core",
        "verif/booley_doctor_selftest/lint_bad.sv",
        "constraints/my_ip.sdc",
        ".booley/booley.toml",
        ".booley/tests.toml",
        ".booley/selftest/sim/bad-overlay/my_ip.sv",
    ):
        assert (tmp_path / rel).is_file(), rel
    reset_cache()
    capsys.readouterr()


def test_step_writes_config_through_a_symlinked_booley_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    symlink_or_skip(project / ".booley", store, target_is_directory=True)
    ctx = _ctx(project, monkeypatch)

    assert step_scaffold(ctx, _args()) is True

    assert ctx.results[-1].status == "ok"
    assert (store / "booley.toml").is_file() and (store / "tests.toml").is_file()
    assert (project / "rtl" / "my_ip.sv").is_file()
    assert (project / ".booley").is_symlink()
    capsys.readouterr()


def test_step_check_only_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch, check_only=True)
    assert step_scaffold(ctx, _args()) is True
    assert ctx.results[-1].status == "warn"
    assert list(tmp_path.iterdir()) == []
    capsys.readouterr()


def _track_scaffold(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "scaffold",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=10)


def _seed_doctor_init_state(root: Path) -> None:
    """Seed state owned by regular init without suppressing project checks."""
    from booley.harness.setup.guidance_links import ensure_guidance_links
    from booley.runtime import devcontainer as dc
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE
    from tests.harness.test_doctor import _seed_interactive

    booley_dir = root / ".booley"
    (booley_dir / "FUSESOC_IGNORE").touch()
    (booley_dir / ".gitignore").write_text(PROJECT_GITIGNORE)
    (booley_dir / "AGENTS.md").write_text("Run Booley Flows inside the Sandbox.\n")
    ensure_guidance_links(root, booley_dir)
    _seed_interactive(root)
    dc.write_devcontainer(
        root, dc.build_devcontainer_spec(dc.APP_CLAUDE, image=doctor.pi.project_image_name(root))
    )
    (root / ".git/info/attributes").unlink()
    (root / ".git/info/exclude").write_text("/.devcontainer\n/home/\n")
    for key, value in (
        ("gc.worktreePruneExpire", "never"),
        ("worktree.useRelativePaths", "true"),
        ("core.autocrlf", "false"),
    ):
        subprocess.run(["git", "config", key, value], cwd=root, check=True, timeout=10)
    config = booley_dir / "booley.toml"
    config.write_text(config.read_text() + '\n[agent]\nprovider = "claude"\nauth = "auto"\n')
    _track_scaffold(root)


def _healthy_host_doctor(root: Path, monkeypatch) -> None:
    """Run all Doctor checks with healthy host/process observations."""
    from tests.harness.test_doctor import _patch_environment

    _seed_doctor_init_state(root)
    real_run = subprocess.run
    _patch_environment(
        monkeypatch,
        root,
        root / ".booley",
        mcp_tools=[
            "synth",
            "bwave",
            "coverage_analyst",
            "lint",
            "mutation_tester",
            "reviewer",
            "sim",
            "fpga",
        ],
    )
    host_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        marker = re.search(r"BOOLEY_VIVADO_STARTUP_[0-9a-f]{32}", " ".join(map(str, command)))
        if marker:
            return subprocess.CompletedProcess(command, 0, marker[0] + "\n", "")
        return host_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = doctor.run_doctor_result(
        argparse.Namespace(deep=False), root, read_only=True, record_clean=False
    )
    assert not [
        (f.check_id, f.subject, f.message)
        for f in result.findings
        if f.severity in {"warn", "fail"}
    ]


def _assert_variant_sources_and_header(tmp_path, simulator, tb_style, asic, fpga):
    rtl = (tmp_path / "rtl/my_ip.sv").read_text()
    header = rtl.split("every configured flow (")[1].split(")")[0]
    enabled = ["simulate", "lint"] + (["synth"] if asic else []) + (["fpga"] if fpga else [])
    assert header.replace("//", "").split() == " / ".join(enabled).split()
    core = yaml.safe_load((tmp_path / "my_ip.core").read_text())
    owned = simulator == "verilator" and tb_style == "sv"
    config = tomllib.loads((tmp_path / ".booley/booley.toml").read_text())
    assert config["flows"]["sim"] == ({"trace_files": ["dump.vcd"]} if owned else {})
    assert ("tb_cpp" in core["filesets"]) is owned
    assert (tmp_path / "tb/sim_main.cpp").exists() is owned
    if owned:
        assert doctor.sim_dump.dump_literals_in((tmp_path / "tb/sim_main.cpp").read_text()) == (
            "dump.vcd",
        )
        assert core["filesets"]["tb_cpp"] == {
            "files": [{"tb/sim_main.cpp": {"file_type": "cppSource"}}],
            "tags": ["tb"],
        }
        assert core["targets"]["sim"]["filesets"] == ["rtl", "tb", "tb_cpp"]
        assert core["targets"]["sim"]["flow_options"]["booley"]["coverage"] == {
            "custom_main_hooks": ["write_hook"]
        }
    catalog = TargetCatalog.build(tmp_path)
    sources = doctor._CoreAuditInputs(catalog, {h.name: h for h in catalog.list()}).sources_for(
        "sim"
    )
    tb = "tb/tb_my_ip.sv" if tb_style == "sv" else "tb/test_my_ip.py"
    assert set(sources.tb_files) == {tb, *(["tb/sim_main.cpp"] if owned else [])}
    assert sources.rtl_source_files == ("rtl/my_ip.sv",)


@pytest.mark.usefixtures("isolated_git_attributes")
@pytest.mark.parametrize("simulator", ["verilator", "icarus"])
@pytest.mark.parametrize("tb_style", ["sv", "cocotb"])
@pytest.mark.parametrize("linter", ["verilator", "verible"])
@pytest.mark.parametrize("asic,fpga", [(False, False), (True, False), (False, True), (True, True)])
def test_scaffold_variants_have_no_doctor_warnings_and_enabled_header(
    tmp_path, monkeypatch, simulator, tb_style, linter, asic, fpga
):
    args = _args(
        sim_eda_tool=simulator,
        tb_style=tb_style,
        lint_eda_tool=linter,
        asic=asic,
        fpga_part="xc7a35tcpg236-1" if fpga else None,
    )
    assert step_scaffold(_ctx(tmp_path, monkeypatch), args)
    _track_scaffold(tmp_path)
    cfg = tmp_path / ".booley"
    project = doctor.ProjectAudit(
        tmp_path,
        cfg,
        tomllib.loads((cfg / "booley.toml").read_text()),
        tomllib.loads((cfg / "tests.toml").read_text()),
        "sim",
    )
    reporter = doctor._Reporter.create()
    doctor._run_core_audit(
        project,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _note=reporter.note_,
    )
    findings = reporter.result(0).findings
    assert not [
        (f.check_id, f.subject, f.message) for f in findings if f.severity in {"warn", "fail"}
    ]
    _assert_variant_sources_and_header(tmp_path, simulator, tb_style, asic, fpga)
    _healthy_host_doctor(tmp_path, monkeypatch)


@pytest.mark.parametrize("category", ["rtl", "tb"])
def test_scaffold_trace_main_isolation(tmp_path, monkeypatch, category):
    from booley.core.models import AgentCallParams
    from booley.specialists.specialist_workspace import isolated_agent_workspace

    assert step_scaffold(_ctx(tmp_path, monkeypatch), _args(asic=False))
    _track_scaffold(tmp_path)
    params = AgentCallParams(prompt="review", model="test", cwd=tmp_path)
    with isolated_agent_workspace(params, "read_only", category) as (isolated, _snapshot):
        root = Path(isolated.cwd)
        assert (root / "rtl/my_ip.sv").exists() is (category == "rtl")
        assert (root / "tb/tb_my_ip.sv").exists() is (category == "tb")
        assert (root / "tb/sim_main.cpp").exists() is (category == "tb")


def test_scaffold_without_fpga_records_disabled_flow():
    data = tomllib.loads(scaffold_files(_choices())[".booley/booley.toml"])
    assert data["flows"].get("fpga", {}).get("enabled") is False, "declined FPGA must be disabled"


# Synthesis intent must survive the scaffold boundary and delivered reporting.
def test_scaffold_synth_mode_cli_contract(tmp_path, monkeypatch):
    from booley.harness.booley import _build_parser

    parser = _build_parser()
    init = parser._subparsers._group_actions[0].choices["init"]
    assert "--synth-mode" in init.format_help(), "scaffold synthesis mode flag is missing"
    ctx = _ctx(tmp_path, monkeypatch)
    for mode in ("logical", "physical"):
        args = parser.parse_args(["init", "--scaffold", "my_ip", "--synth-mode", mode])
        assert gather_scaffold_choices(args, ctx).synth_mode == mode
    assert (
        gather_scaffold_choices(parser.parse_args(["init", "--scaffold", "my_ip"]), ctx).synth_mode
        == "physical"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["init", "--synth-mode", "invalid"])


def test_scaffold_logical_synthesis_has_no_sdc(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    args = _args(synth_mode="logical", fpga_part="xc7a200tfbg484-1")
    choices = gather_scaffold_choices(args, ctx)
    assert getattr(choices, "synth_mode", None) == "logical", "logical scaffold mode was dropped"
    assert step_scaffold(ctx, args)
    core = read_core(tmp_path / "my_ip.core")
    assert core["targets"]["synth"]["flow_options"]["synth_mode"] == "logical"
    assert core["targets"]["synth"]["filesets"] == ["rtl"]
    assert "constraints" not in core["filesets"]
    assert not (tmp_path / "constraints/my_ip.sdc").exists()
    assert (tmp_path / "constraints/my_ip.xdc").is_file()
    assert core_schema_errors(tmp_path / "my_ip.core") == []
    assert "synth" in {h.name for h in TargetCatalog.build(tmp_path).list()}
    resolved = _resolve_target("synth", project_root=tmp_path, build_root=tmp_path / "build")
    assert resolved.eda_tool == "yosys"
    _track_scaffold(tmp_path)
    cfg = tmp_path / ".booley"
    project = doctor.ProjectAudit(
        tmp_path,
        cfg,
        tomllib.loads((cfg / "booley.toml").read_text()),
        tomllib.loads((cfg / "tests.toml").read_text()),
        "sim",
    )
    reporter = doctor._Reporter.create()
    doctor._run_core_audit(
        project,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _note=reporter.note_,
    )
    assert not [f for f in reporter.result(0).findings if f.severity in {"warn", "fail"}]


def test_scaffold_physical_default_and_disabled_synthesis(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    for mode in (None, "physical"):
        files = scaffold_files(gather_scaffold_choices(_args(synth_mode=mode), ctx))
        core = _core(files)
        assert core["targets"]["synth"]["flow_options"]["synth_mode"] == "physical"
        assert "constraints" in core["targets"]["synth"]["filesets"]
        assert "constraints/my_ip.sdc" in files
    files = scaffold_files(gather_scaffold_choices(_args(asic=False), ctx))
    assert "synth" not in _core(files)["targets"]
    assert "constraints/my_ip.sdc" not in files


def test_scaffold_rejects_explicit_mode_with_no_asic(tmp_path, monkeypatch, capsys):
    from booley.harness.setup import scaffold

    ctx = _ctx(tmp_path, monkeypatch)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for mode in ("logical", "physical"):
        ctx.interactive = False
        args = _args(asic=False, synth_mode=mode)
        assert gather_scaffold_choices(args, ctx) is None, (
            "explicit synthesis mode with no ASIC was accepted"
        )
        assert not step_scaffold(ctx, args)
        assert ctx.results[-1].status == "err"
        assert {
            p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
        } == before
        ctx.interactive = True
        monkeypatch.setattr(scaffold, "_ask_yes_no", lambda *a, **k: False)
        assert (
            gather_scaffold_choices(
                _args(
                    asic=None,
                    synth_mode=mode,
                    sim_eda_tool="verilator",
                    tb_style="sv",
                    lint_eda_tool="verilator",
                ),
                ctx,
            )
            is None
        )
    output = capsys.readouterr()
    assert "--synth-mode" in output.err + output.out
    assert "--no-asic" in output.err + output.out


def test_scaffold_logical_request_reaches_synthesis_report(tmp_path, monkeypatch):
    import json

    from booley.flows.base import SubprocessResult
    from booley.flows.endpoint_reporting import write_report
    from booley.flows.synth.backends import configure, pipeline
    from booley.flows.synth.flow import AsicSynthesizeFlow

    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args(synth_mode="logical"))
    commands = []

    def configure_actual(self, target, cmd):
        commands.append(cmd)
        parsed = configure.parse_configure_argv(cmd)
        spec = configure.resolve_spec(parsed, project_root=tmp_path, require_liberty=False)
        build = self._synth_build_dir(target)
        build.mkdir(parents=True, exist_ok=True)
        return pipeline.SynthPlan(build_dir=build, spec=spec)

    monkeypatch.setattr(AsicSynthesizeFlow, "_configure_synth", configure_actual)
    monkeypatch.setattr(
        AsicSynthesizeFlow,
        "_execute_boundary",
        lambda *a, **k: SubprocessResult(
            returncode=0,
            stdout=(
                "Chip area for top module 'my_ip': 1000.0\nNumber of cells: 10\n"
                "STA_WORST_SLACK_NS: 0.500000\nOPENROAD_DESIGN_AREA_UM2: 1000.0\n"
                "STA_PERCLOCK: name=clk period_ns=10.0 wns_ns=0.5 whs_ns=0.1\n"
            ),
            stderr="",
        ),
    )
    flow = AsicSynthesizeFlow()
    flow.parse_args(
        [
            "--target",
            "synth",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    flow.read_state()
    result = flow._run()
    path = write_report(flow, result)
    report = json.loads(path.read_text())
    assert report["passed"] is True
    assert report["detail"]["synth"]["synth_mode"] == "logical", (
        "scaffold request did not reach the delivered synthesis report"
    )
    assert path.parent.name.isdigit()
    assert report["target"] == "synth"
    assert report["eda_tool"] == "yosys"
    assert report["passed"] is True
    assert report["detail"]["synth"]["area_source"] == "yosys_mapped"
    assert commands[0][commands[0].index("--synth-mode") + 1] == "logical"


def test_scaffold_interactive_synthesis_mode(tmp_path, monkeypatch):
    from booley.harness.setup import scaffold

    ctx = _ctx(tmp_path, monkeypatch)
    ctx.interactive = True
    questions = []

    def choice(question, options, default):
        questions.append(question)
        return "logical" if "synthesis mode" in question else default

    monkeypatch.setattr(scaffold, "_ask_choice", choice)
    args = _args(asic=True, fpga_part="xc7a200tfbg484-1")
    assert gather_scaffold_choices(args, ctx).synth_mode == "logical"
    assert any("synthesis mode" in q for q in questions)
    questions.clear()
    args.synth_mode = "physical"
    assert gather_scaffold_choices(args, ctx).synth_mode == "physical"
    assert not any("synthesis mode" in q for q in questions)
    args.asic, args.synth_mode = False, None
    assert gather_scaffold_choices(args, ctx).asic is False
    assert not any("synthesis mode" in q for q in questions)


# ---------------------------------------------------------------------------
# --stealth: Booley-authored cores live under .booley/cores/ (issue #2332)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout


def _stealth_scaffold(root: Path, monkeypatch) -> InitContext:
    """Scaffold with ``--stealth`` and run the init steps that own stealth state.

    Deliberately no Interactive step: the hidden footprint must not depend on
    it succeeding (an offline init returns before excluding anything).
    """
    from booley.harness import init_cmd

    _git(root, "init", "-q")
    ctx = _ctx(root, monkeypatch)
    assert step_scaffold(ctx, _args(stealth=True)) is True
    init_cmd._step_booley_dir(ctx)
    init_cmd._step_core_projections(ctx)
    reset_cache()
    return ctx


def test_stealth_scaffold_tracks_no_booley_cores(tmp_path: Path, monkeypatch, capsys) -> None:
    _stealth_scaffold(tmp_path, monkeypatch)

    cfg = tomllib.loads((tmp_path / ".booley/booley.toml").read_text(encoding="utf-8"))
    assert cfg["stealth"]["enabled"] is True
    assert (tmp_path / ".booley/cores/my_ip.core").is_file()
    assert (tmp_path / ".booley/cores/booley_doctor_selftest.core").is_file()
    assert (tmp_path / ".booley/cores/booley_doctor_selftest/lint_bad.sv").is_file()
    assert not (tmp_path / "my_ip.core").exists()
    assert not (tmp_path / "verif").exists()
    assert (tmp_path / ".booley/.git").exists()

    _git(tmp_path, "add", "-A")
    tracked = _git(tmp_path, "ls-files").splitlines()
    assert not [path for path in tracked if path.endswith(".core")], tracked
    assert not [path for path in tracked if path.split("/")[0] == ".booley"], tracked
    assert "rtl/my_ip.sv" in tracked
    capsys.readouterr()


def test_stealth_scaffold_targets_resolve_and_audit_clean(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pytest.importorskip("fusesoc")
    from booley.harness.setup import readiness

    _stealth_scaffold(tmp_path, monkeypatch)
    booley_dir = tmp_path / ".booley"
    _git(tmp_path, "add", "-A")
    _git(booley_dir, "add", "-A")
    _git(booley_dir, "commit", "-qm", "stealth files")
    project = doctor.ProjectAudit(
        tmp_path,
        booley_dir,
        tomllib.loads((booley_dir / "booley.toml").read_text()),
        tomllib.loads((booley_dir / "tests.toml").read_text()),
        "sim",
    )

    for target in ("sim", "lint"):
        _resolve_target(target, project_root=tmp_path, build_root=tmp_path / "build" / target)
    bad = _resolve_target(
        "lint_selftest_bad", project_root=tmp_path, build_root=tmp_path / "build" / "bad"
    )
    assert bad.vlnv == "::my_ip_booley_doctor_selftest:0"
    assert bad.rtl_source_files[0].absolute(bad.build_root).is_file()

    warnings: list[str] = []
    assert doctor._selftest_plan(project, "sim", warnings.append) is not None
    assert doctor._selftest_plan(project, "lint", warnings.append) is not None
    assert warnings == []
    sim = _resolve_target("sim", project_root=tmp_path, build_root=tmp_path / "build" / "sim2")
    assert selftest_overlay.stage_bad_overlay(booley_dir, "sim", sim.build_root) == 1

    reporter = doctor._Reporter.create()
    doctor._run_core_audit(
        project,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _note=reporter.note_,
    )
    findings = list(reporter.result(0).findings)
    findings += readiness.check_stealth_cores(
        project, mode=readiness.ReadinessMode.RECONCILE
    ).findings
    assert not [
        (f.check_id, f.subject, f.message) for f in findings if f.severity in {"warn", "fail"}
    ]
    capsys.readouterr()
