"""Tests for ``booley init --scaffold`` (init_scaffold).

The generators are pure (choices in, files out), so most combos are validated
without touching disk; the step-level tests exercise the refusal gate and the
actual writes in a tmp repo.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest
import yaml

from booley.fusesoc_registry import enumerate_targets, read_core
from booley.harness.init_cmd import BOOLEY_TOML_SKELETON, TESTS_TOML_SKELETON
from booley.harness.init_common import InitContext
from booley.harness.init_scaffold import (
    ScaffoldChoices,
    existing_design_files,
    gather_scaffold_choices,
    scaffold_files,
    step_scaffold,
)
from booley.project_dir import reset_cache


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
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _ctx(project_root: Path, monkeypatch, **kw) -> InitContext:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    return InitContext(project_root=project_root, interactive=False, **kw)


def _core(files: dict[str, str]) -> dict:
    return yaml.safe_load(files["my_ip.core"])


# ---------------------------------------------------------------------------
# Generators — per-combo shapes
# ---------------------------------------------------------------------------


def test_default_combo_files_and_shapes() -> None:
    files = scaffold_files(_choices())

    assert set(files) == {
        "rtl/my_ip.sv",
        "tb/tb_my_ip.sv",
        "my_ip.core",
        "constraints/my_ip.sdc",
        ".booley_project/booley.toml",
        ".booley_project/tests.toml",
    }

    core = _core(files)
    assert core["name"] == "::my_ip:0"
    sim = core["targets"]["sim"]
    assert sim["flow"] == "sim"
    assert sim["flow_options"]["tool"] == "verilator"
    # Event-driven TB needs --timing; --main/--exe build the C++ wrapper.
    assert sim["flow_options"]["verilator_options"] == ["--timing", "--main", "--exe"]
    assert sim["toplevel"] == "tb_my_ip"
    assert core["filesets"]["tb"]["tags"] == ["tb"]

    lint = core["targets"]["lint"]
    assert lint["flow"] == "lint"
    assert lint["flow_options"] == {"tool": "verilator"}
    assert lint["toplevel"] == "my_ip"

    synth = core["targets"]["synth"]
    assert synth["flow"] == "generic"
    assert synth["flow_options"] == {"tool": "yosys", "arch": "xilinx"}
    assert "constraints" in synth["filesets"]
    sdc_entry = core["filesets"]["constraints"]["files"][0]
    assert sdc_entry == {"constraints/my_ip.sdc": {"file_type": "SDC"}}

    cfg = tomllib.loads(files[".booley_project/booley.toml"])
    assert cfg["project"]["name"] == "my_ip"
    # Stealth is OFF in a scaffolded repo: it exists to exercise Booley, so
    # the commit sanitizer would redact its own MCP tool name from subjects and
    # drop multi-line bodies for no privacy gain.
    assert cfg["stealth"] == {"enabled": False}
    # ADR 0039: one builder, so no `backend` line — just the Target (and venue
    # where it differs from the default). `enabled = true` is implicit.
    assert cfg["flows"]["sim"] == {"default_target": "sim"}
    assert cfg["flows"]["lint"] == {"default_target": "lint"}
    # Elaborate reuses the (cheap) verilator lint Target.
    assert cfg["flows"]["elab"] == {"default_target": "lint"}
    assert cfg["flows"]["synth"] == {"default_target": "synth"}
    assert "fpga" not in cfg["flows"]

    tests_cfg = tomllib.loads(files[".booley_project/tests.toml"])
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
    assert opts == {"tool": "icarus", "iverilog_options": ["-g2012"]}


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

    tests_cfg = tomllib.loads(files[".booley_project/tests.toml"])
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


def test_verible_lint_falls_back_to_sim_for_elaborate() -> None:
    files = scaffold_files(_choices(lint_eda_tool="verible"))
    core = _core(files)
    assert core["targets"]["lint"]["flow_options"] == {"tool": "verible"}

    cfg = tomllib.loads(files[".booley_project/booley.toml"])
    # verible can't elaborate; the sandbox sim Target is the fallback.
    assert cfg["flows"]["elab"] == {"default_target": "sim"}


def test_verible_lint_reuses_sim_for_elaborate() -> None:
    files = scaffold_files(_choices(lint_eda_tool="verible"))
    cfg = tomllib.loads(files[".booley_project/booley.toml"])
    assert cfg["flows"]["elab"] == {"default_target": "sim"}


def test_no_asic_omits_synth_target_and_sdc() -> None:
    files = scaffold_files(_choices(asic=False))
    assert "constraints/my_ip.sdc" not in files
    core = _core(files)
    assert "synth" not in core["targets"]
    assert "constraints" not in core["filesets"]
    cfg = tomllib.loads(files[".booley_project/booley.toml"])
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
        "out_of_context": True,
    }
    assert "xdc" in fpga["filesets"]
    xdc_entry = core["filesets"]["xdc"]["files"][0]
    assert xdc_entry == {"constraints/my_ip.xdc": {"file_type": "xdc"}}

    cfg = tomllib.loads(files[".booley_project/booley.toml"])
    assert "backend" not in cfg["flows"]["fpga"]
    assert cfg["flows"]["fpga"]["default_target"] == "fpga"


@pytest.mark.parametrize("tb_style", ["sv", "cocotb"])
@pytest.mark.parametrize("sim_eda_tool", ["verilator", "icarus"])
def test_every_combo_parses_with_booleys_own_readers(
    tmp_path: Path, monkeypatch, sim_eda_tool: str, tb_style: str
) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
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

    targets = enumerate_targets(tmp_path)
    expected = {"sim", "lint", "synth", "fpga"}
    assert expected <= set(targets)
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
    stealth = tmp_path / ".booley_project" / "cores"
    stealth.mkdir(parents=True)
    (stealth / "stealth.core").write_text("CAPI=2:\n")

    found = {p.as_posix() for p in existing_design_files(tmp_path)}
    assert found == {"rtl/old.v", "chip.core", ".booley_project/cores/stealth.core"}


def test_step_refuses_on_existing_design(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    assert not (tmp_path / "rtl").exists()
    capsys.readouterr()


def test_step_refuses_on_populated_config(tmp_path: Path, monkeypatch, capsys) -> None:
    pd = tmp_path / ".booley_project"
    pd.mkdir()
    (pd / "booley.toml").write_text('[project]\nname = "existing"\n')
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    capsys.readouterr()


def test_step_overwrites_comment_only_skeletons(tmp_path: Path, monkeypatch, capsys) -> None:
    # A prior plain `booley init` leaves comment-only skeletons; scaffolding
    # over them is the supported "ran init first, then decided to scaffold" path.
    pd = tmp_path / ".booley_project"
    pd.mkdir()
    (pd / "booley.toml").write_text(BOOLEY_TOML_SKELETON)
    (pd / "tests.toml").write_text(TESTS_TOML_SKELETON)
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
        "constraints/my_ip.sdc",
        ".booley_project/booley.toml",
        ".booley_project/tests.toml",
    ):
        assert (tmp_path / rel).is_file(), rel
    reset_cache()
    capsys.readouterr()


def test_step_check_only_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _ctx(tmp_path, monkeypatch, check_only=True)
    assert step_scaffold(ctx, _args()) is True
    assert ctx.results[-1].status == "warn"
    assert list(tmp_path.iterdir()) == []
    capsys.readouterr()
