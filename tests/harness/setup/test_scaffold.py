"""Tests for ``booley init --scaffold`` in the setup.scaffold module.

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

from booley.audit import target_matrix
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.fusesoc_registry import (
    core_schema_errors,
    read_core,
)
from booley.harness import doctor, init_cmd
from booley.harness.init_cmd import BOOLEY_TOML_SKELETON, TESTS_TOML_SKELETON
from booley.harness.setup.common import InitContext
from booley.harness.setup.guidance_links import ensure_guidance_links
from booley.harness.setup.scaffold import (
    ScaffoldChoices,
    existing_design_files,
    gather_scaffold_choices,
    scaffold_files,
    step_scaffold,
)
from booley.runtime import devcontainer as dc
from booley.runtime.booley_dir import init_booley_dir_scope, reset_cache
from booley.targets.catalog import TargetCatalog
from booley.targets.target_surface import collect_surface
from tests.conftest import symlink_or_skip
from tests.devcontainer_spec_support import build_spec
from tests.docker_cli_guard import route_docker_helper_through_subprocess_run
from tests.harness.doctor_host_support import serve_healthy_doctor_host
from tests.subprocess_budget import run_bounded_child


def _resolve(root, selector, build_root, *, doctor_private=False):
    with pytest.MonkeyPatch.context() as patch:
        if doctor_private:
            patch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        handle = TargetCatalog.build(root).select(selector)
    return fusesoc_registry.resolve_target_handle(handle, build_root=build_root)


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)


def _choices(**overrides) -> ScaffoldChoices:
    defaults = {
        "name": "my_ip",
        "sim_eda_tool": "verilator",
        "tb_style": "sv",
        "lint_eda_tool": "verilator",
        "asic": True,
        "fpga_part": None,
        "style_lint": True,  # the CLI default
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
        "style_lint": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _ctx(project_root: Path, monkeypatch, **kw) -> InitContext:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    return InitContext(project_root=project_root, interactive=False, **kw)


def _core(files: dict[str, str]) -> dict:
    return yaml.safe_load(files["my_ip.core"])


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")


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
    sim = core["targets"]["sim_my_ip"]
    assert sim["flow"] == "sim"
    assert sim["flow_options"]["tool"] == "verilator"
    # The owned C++ main drives the event scheduler and trace lifecycle.
    assert sim["flow_options"]["verilator_options"] == ["--timing", "--exe"]
    assert sim["toplevel"] == "tb_my_ip"
    assert core["filesets"]["tb"]["tags"] == ["tb"]

    lint = core["targets"]["lint_my_ip"]
    assert lint["flow"] == "lint"
    assert lint["flow_options"] == {
        "tool": "verilator",
        "verilator_options": ["-Wall"],
        "booley": {"doctor": ["lint"]},
    }
    assert lint["toplevel"] == "my_ip"
    assert core["targets"]["lint_my_ip_style"] == {
        "flow": "lint",
        "flow_options": {"tool": "verible", "booley": {"doctor": ["lint"]}},
        "filesets": ["rtl"],
        "toplevel": "my_ip",
    }

    synth = core["targets"]["synth_my_ip"]
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
    # the commit-msg hook would reject its own MCP tool name in subjects for no
    # privacy gain.
    assert cfg["stealth"] == {"enabled": False}
    # Target selection lives beside each Target in the .core file.
    assert cfg["flows"]["sim"] == {"trace_files": ["dump.vcd"]}
    assert cfg["flows"]["lint"] == {}
    assert "elab" not in cfg["flows"]
    assert cfg["flows"]["synth"] == {}
    assert sim["flow_options"]["booley"]["doctor"] == ["sim"]
    assert cfg["flows"]["fpga"] == {"enabled": False}

    tests_cfg = tomllib.loads(files[".booley/tests.toml"])
    assert tests_cfg["sim_my_ip"]["tests"] == ["reset", "count"]
    assert tests_cfg["sim_my_ip"]["select"] == "+test_id={index}"

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


@pytest.mark.parametrize(
    ("lint_eda_tool", "style_lint", "lint_targets"),
    [
        ("verilator", True, ("lint_my_ip", "lint_my_ip_style")),
        ("verilator", False, ("lint_my_ip",)),
        ("verible", False, ("lint_my_ip",)),
    ],
)
def test_scaffold_supplies_doctor_fail_path_fixtures(
    tmp_path: Path, monkeypatch, lint_eda_tool: str, style_lint: bool, lint_targets: tuple
) -> None:
    """Doctor's --deep self-test finds both fixtures; only Doctor sees the bad lint Target."""
    files = scaffold_files(_choices(lint_eda_tool=lint_eda_tool, style_lint=style_lint))
    _write(tmp_path, files)

    assert selftest_overlay.has_bad_overlay(tmp_path / ".booley", "sim")
    assert target_matrix.doctor_targets(tmp_path, "lint") == lint_targets
    core = _core(files)
    assert "lint_selftest_bad" not in core["targets"]
    selftest_core = yaml.safe_load(files["verif/booley_doctor_selftest.core"])
    lint_bad = selftest_core["targets"]["lint_selftest_bad"]
    assert lint_bad == {
        "flow": "lint",
        "flow_options": {"tool": lint_eda_tool, "booley": {"doctor_selftest": True}},
        "filesets": ["lint_selftest_bad"],
        "toplevel": "my_ip_lint_selftest_bad",
    }
    # An HDL file type keeps Lint from excluding the bad source it must fail on.
    assert selftest_core["filesets"]["lint_selftest_bad"]["files"] == [
        {"booley_doctor_selftest/lint_bad.sv": {"file_type": "systemVerilogSource"}}
    ]
    assert "lint_selftest_bad" not in core["filesets"]
    assert core_schema_errors(tmp_path / "verif/booley_doctor_selftest.core") == []

    public = {handle.name for handle in TargetCatalog.build(tmp_path).list()}
    assert {"sim_my_ip", "synth_my_ip", *lint_targets} <= public
    assert "lint_selftest_bad" not in public
    assert "lint_selftest_bad" not in {entry.name for entry in collect_surface(tmp_path).entries()}
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    assert TargetCatalog.build(tmp_path).select("lint_selftest_bad").doctor_private


def test_scaffold_sim_bad_overlay_replaces_the_staged_rtl(tmp_path: Path) -> None:
    pytest.importorskip("fusesoc")
    files = scaffold_files(_choices())
    _write(tmp_path, files)

    resolved = _resolve(tmp_path, "sim_my_ip", tmp_path / "build")
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
    _write(tmp_path, scaffold_files(_choices()))

    resolved = _resolve(tmp_path, "lint_selftest_bad", tmp_path / "build", doctor_private=True)

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


@pytest.mark.parametrize(
    ("overrides", "target", "flow_options"),
    [
        pytest.param(
            {"sim_eda_tool": "icarus"},
            "sim_my_ip",
            {"tool": "icarus", "booley": {"doctor": ["sim"]}, "iverilog_options": ["-g2012"]},
            id="icarus-sim",
        ),
        # The RTL is SystemVerilog and iverilog defaults to Verilog-2005: a non-sim
        # Icarus Target is where -g2012 got forgotten, manufacturing syntax errors that
        # read like design bugs. The rule is keyed on the EDA tool, not the Flow.
        pytest.param(
            {"lint_eda_tool": "icarus"},
            "lint_my_ip",
            {"tool": "icarus", "iverilog_options": ["-g2012"], "booley": {"doctor": ["lint"]}},
            id="icarus-lint",
        ),
        pytest.param(
            {"tb_style": "cocotb"},
            "sim_my_ip",
            {
                "tool": "verilator",
                "booley": {"doctor": ["sim"]},
                "cocotb_module": "test_my_ip",
                "timescale": "1ns/1ps",
                "verilator_options": ["--timing", "-Wno-fatal"],
            },
            id="cocotb-verilator",
        ),
        pytest.param(
            {"lint_eda_tool": "verible", "style_lint": False},
            "lint_my_ip",
            {"tool": "verible", "booley": {"doctor": ["lint"]}},
            id="verible-lint",
        ),
        pytest.param(
            {"style_lint": False},
            "lint_my_ip",
            {"tool": "verilator", "verilator_options": ["-Wall"], "booley": {"doctor": ["lint"]}},
            id="style-lint-off",
        ),
        pytest.param(
            {"fpga_part": "xc7a200tfbg484-1"},
            "fpga_my_ip",
            {
                "tool": "vivado",
                "part": "xc7a200tfbg484-1",
                "ppa_profile": "balanced",
                "out_of_context": True,
                "booley": {"doctor": ["fpga"]},
            },
            id="fpga",
        ),
    ],
)
def test_choice_sets_target_flow_options(overrides: dict, target: str, flow_options: dict) -> None:
    assert _core(scaffold_files(_choices(**overrides)))["targets"][target]["flow_options"] == (
        flow_options
    )


def test_cocotb_combo_shapes() -> None:
    files = scaffold_files(_choices(sim_eda_tool="icarus", tb_style="cocotb"))
    assert "tb/test_my_ip.py" in files
    assert "tb/tb_my_ip.sv" not in files

    core = _core(files)
    sim = core["targets"]["sim_my_ip"]
    assert sim["flow_options"]["cocotb_module"] == "test_my_ip"
    assert sim["flow_options"]["iverilog_options"] == ["-g2012"]
    assert sim["toplevel"] == "my_ip"  # the DUT itself — no HDL wrapper
    tb_entry = core["filesets"]["tb"]["files"][0]
    assert tb_entry == {"tb/test_my_ip.py": {"file_type": "user", "copyto": "test_my_ip.py"}}

    tests_cfg = tomllib.loads(files[".booley/tests.toml"])
    assert tests_cfg["sim_my_ip"]["tests"] == ["test_reset", "test_count"]
    # A select plusarg on a Cocotb Target is a setup-time error — never emit one.
    assert "select" not in tests_cfg["sim_my_ip"]

    tb = files["tb/test_my_ip.py"]
    assert "@cocotb.test()" in tb
    assert "[SIM_RESULT]" not in tb  # cocotb verdicts come from results.xml


def test_fpga_part_adds_xdc_and_enables_the_fpga_flow() -> None:
    files = scaffold_files(_choices(fpga_part="xc7a200tfbg484-1"))
    core = _core(files)
    assert core["targets"]["fpga_my_ip"]["flow"] == "generic"
    assert "xdc" in core["targets"]["fpga_my_ip"]["filesets"]
    assert core["filesets"]["xdc"]["files"] == [{"constraints/my_ip.xdc": {"file_type": "xdc"}}]
    assert "constraints/my_ip.xdc" in files
    assert tomllib.loads(files[".booley/booley.toml"])["flows"]["fpga"] == {}


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


def test_gather_reports_defaults_without_prompting(tmp_path: Path, monkeypatch, capsys) -> None:
    """Init asks no design question, even on a TTY: each default is reported instead."""
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.interactive = True

    def no_prompt(*_args, **_kwargs):
        raise AssertionError("scaffold prompted for a design choice")

    monkeypatch.setattr("builtins.input", no_prompt)
    choices = gather_scaffold_choices(_args(), ctx)
    assert choices == ScaffoldChoices(
        name="my_ip",
        sim_eda_tool="verilator",
        tb_style="sv",
        lint_eda_tool="verilator",
        asic=True,
        fpga_part=None,
        style_lint=True,
    )
    out = capsys.readouterr().out
    for line in (
        "Style lint: Verible Target lint_my_ip_style (default; set --no-style-lint to skip)",
        "Simulator: verilator (default; set --sim-eda-tool)",
        "Testbench style: sv (default; set --tb-style)",
        "Lint EDA tool: verilator (default; set --lint-eda-tool)",
        "ASIC synthesis: enabled (default; set --no-asic to skip)",
        "Synthesis mode: physical (default; set --synth-mode)",
        "FPGA implementation: skipped (default; set --fpga-part to enable)",
        "Stealth: off (default; set --stealth)",
    ):
        assert line in out


def test_gather_no_style_lint_opts_out(tmp_path: Path, monkeypatch, capsys) -> None:
    choices = gather_scaffold_choices(_args(style_lint=False), _ctx(tmp_path, monkeypatch))
    assert choices is not None
    assert choices.style_lint is False
    assert "Style lint:" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", [True, False])
def test_gather_rejects_style_lint_flag_with_verible(
    tmp_path: Path, monkeypatch, capsys, flag: bool
) -> None:
    args = _args(lint_eda_tool="verible", style_lint=flag)
    assert gather_scaffold_choices(args, _ctx(tmp_path, monkeypatch)) is None
    assert "--lint-eda-tool verible" in capsys.readouterr().out


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
    # Linked worktrees under a visible relocated Booley Directory are not this checkout's.
    (tmp_path / "booley.toml").write_text('[project]\ndir = "state"\n')
    for copy in ("t3-worktrees/work/t3-1/rtl/copy.core", "worktrees/s1/x.sv"):
        (tmp_path / "state" / copy).parent.mkdir(parents=True)
        (tmp_path / "state" / copy).write_text("copy\n")

    found = {p.as_posix() for p in existing_design_files(tmp_path)}
    assert found == {"rtl/old.v", "chip.core", ".booley/cores/stealth.core"}
    # A [project].dir outside the checkout is allowed and skips nothing inside it.
    (tmp_path / "booley.toml").write_text('[project]\ndir = "../outside"\n')
    assert Path("state/worktrees/s1/x.sv") in existing_design_files(tmp_path)


def test_step_refuses_on_existing_design(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    assert not (tmp_path / "rtl").exists()
    capsys.readouterr()


@pytest.mark.parametrize(
    "content",
    [
        '[project]\nname = "existing"\n',
        '[project]\nname = "existing"\n\n[agent]\nprovider = "claude"\n',
    ],
    ids=["project", "project-and-agent"],
)
def test_step_refuses_on_populated_config(
    tmp_path: Path, monkeypatch, capsys, content: str
) -> None:
    pd = tmp_path / ".booley"
    pd.mkdir()
    (pd / "booley.toml").write_text(content)
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is False
    assert ctx.results[-1].status == "err"
    assert (pd / "booley.toml").read_text() == content
    capsys.readouterr()


def test_step_keeps_agent_table_from_plain_init(tmp_path: Path, monkeypatch, capsys) -> None:
    """The `[agent]` table plain init wrote is not authored config: the scaffold keeps it."""
    pd = tmp_path / ".booley"
    pd.mkdir()
    (pd / "booley.toml").write_text(
        "# booley-init-origin: port\n"
        + BOOLEY_TOML_SKELETON
        + '\n[agent]\ndefault = "codex"\nauth = "api_key"\n',
        encoding="utf-8",
    )
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is True
    text = (pd / "booley.toml").read_text(encoding="utf-8")
    cfg = tomllib.loads(text)
    assert cfg["agent"] == {"default": "codex", "auth": "api_key"}
    assert cfg["project"]["name"] == "my_ip"
    assert "# booley-init-origin: scaffold" in text
    assert "# booley-init-origin: port" not in text
    capsys.readouterr()


@pytest.mark.parametrize(
    "agent_toml",
    [
        'agent = { provider = "codex", auth = "api_key" }\n',
        'agent.provider = "codex"\nagent.auth = "api_key"\n',
    ],
    ids=["inline", "dotted"],
)
def test_step_keeps_hand_written_agent_forms_as_a_table(
    tmp_path: Path, monkeypatch, capsys, agent_toml: str
) -> None:
    """An inline or dotted `agent` must not land inside the trailing `[flows.fpga]` table."""
    pd = tmp_path / ".booley"
    pd.mkdir()
    (pd / "booley.toml").write_text(agent_toml, encoding="utf-8")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, _args()) is True
    cfg = tomllib.loads((pd / "booley.toml").read_text(encoding="utf-8"))
    assert cfg["agent"] == {"provider": "codex", "auth": "api_key"}
    assert "agent" not in cfg["flows"]["fpga"]
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
    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert written == set(scaffold_files(_choices()))
    reset_cache()
    # The closing hint names the real Target, not a bare axis.
    assert "target 'sim_my_ip'" in capsys.readouterr().out


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


def _git(root: Path, *args: str) -> str:
    result = run_bounded_child(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        check=True,
    )
    return result.stdout


def _track_scaffold(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "scaffold")


def _initialize(root: Path, ctx: InitContext) -> None:
    """Run Project Initialization the way `booley init` does after the scaffold step.

    No Interactive files: a hidden footprint must not depend on that step succeeding
    (an offline init returns before excluding anything).
    """
    request = init_cmd.ProjectInitialization(
        None,
        root / ".booley" / "booley.toml",
        lambda: init_cmd.ProjectFacts((2, 53, 0)),
        reconcile_interactive_files=False,
    )
    with init_booley_dir_scope(root):
        assert init_cmd.initialize_project(ctx, request)
    reset_cache()


def _doctor_findings(root: Path, monkeypatch) -> list:
    """Add what host init adds after Project Initialization, run Doctor, return its findings.

    The host and its commands are healthy, so no finding may warn or fail.
    """
    (root / ".booley" / "AGENTS.md").write_bytes(b"Run Booley Flows inside the Sandbox.\n")
    ensure_guidance_links(root, root / ".booley")  # the setup skill's guidance step
    dc.write_devcontainer(
        root, build_spec(dc.APP_CLAUDE, image=doctor.pi.project_image_name(root))
    )
    with (root / ".git/info/exclude").open("a", encoding="utf-8") as exclude:
        exclude.write("/.devcontainer\n/home/\n")  # the fake host's home is <root>/home
    config = root / ".booley" / "booley.toml"
    # LF bytes: write_text would turn the tracked file's LF into CRLF on Windows, which
    # Doctor reports as a CRLF checkout.
    config.write_bytes(
        config.read_bytes() + b'\n[agent]\ndefault = "claude"\nauth = "subscription"\n'
    )
    serve_healthy_doctor_host(monkeypatch, root)
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), root, record_clean=False)
    assert not [
        (f.check_id, f.subject, f.message)
        for f in result.findings
        if f.severity in {"warn", "fail"}
    ]
    return list(result.findings)


def _assert_variant_sources_and_header(tmp_path, simulator, tb_style, linter, asic, fpga):
    rtl = (tmp_path / "rtl/my_ip.sv").read_text()
    header = rtl.split("every configured flow (")[1].split(")")[0]
    enabled = ["simulate", "lint"] + (["synth"] if asic else []) + (["fpga"] if fpga else [])
    assert header.replace("//", "").split() == " / ".join(enabled).split()
    core = yaml.safe_load((tmp_path / "my_ip.core").read_text())
    lint_targets = {name for name in core["targets"] if name.startswith("lint_")}
    assert lint_targets == (
        {"lint_my_ip", "lint_my_ip_style"} if linter == "verilator" else {"lint_my_ip"}
    )
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
        assert core["targets"]["sim_my_ip"]["filesets"] == ["rtl", "tb", "tb_cpp"]
        assert core["targets"]["sim_my_ip"]["flow_options"]["booley"]["coverage"] == {
            "custom_main_hooks": ["write_hook"]
        }
    catalog = TargetCatalog.build(tmp_path)
    sources = catalog.inspect(catalog.select("sim_my_ip")).sources
    tb = "tb/tb_my_ip.sv" if tb_style == "sv" else "tb/test_my_ip.py"
    assert set(sources.tb_files) == {tb, *(["tb/sim_main.cpp"] if owned else [])}
    assert sources.rtl_source_files == ("rtl/my_ip.sv",)


@pytest.mark.usefixtures("isolated_git_attributes")
@pytest.mark.parametrize("simulator", ["verilator", "icarus"])
@pytest.mark.parametrize("tb_style", ["sv", "cocotb"])
@pytest.mark.parametrize("linter", ["verilator", "verible"])
@pytest.mark.parametrize(
    "asic,synth_mode,fpga",
    [(False, None, False), (True, None, False), (False, None, True), (True, "logical", True)],
)
def test_scaffold_variants_have_no_doctor_warnings_and_enabled_header(
    tmp_path, monkeypatch, simulator, tb_style, linter, asic, synth_mode, fpga
):
    args = _args(
        sim_eda_tool=simulator,
        tb_style=tb_style,
        lint_eda_tool=linter,
        asic=asic,
        synth_mode=synth_mode,
        fpga_part="xc7a35tcpg236-1" if fpga else None,
    )
    resolved = gather_scaffold_choices(args, _ctx(tmp_path, monkeypatch))
    assert resolved is not None
    assert resolved.style_lint is (linter == "verilator")
    ctx = _ctx(tmp_path, monkeypatch)
    assert step_scaffold(ctx, args)
    _assert_variant_sources_and_header(tmp_path, simulator, tb_style, linter, asic, fpga)
    _git(tmp_path, "init", "-q")
    _initialize(tmp_path, ctx)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "scaffold")
    _doctor_findings(tmp_path, monkeypatch)


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


# Synthesis intent must survive the scaffold boundary and delivered reporting.
def test_scaffold_synth_mode_cli_contract(tmp_path, monkeypatch):
    # Private: `main()` builds this parser and then runs the whole init; nothing public
    # returns the parser `booley init` parses with.
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
    assert core["targets"]["synth_my_ip"]["flow_options"]["synth_mode"] == "logical"
    assert core["targets"]["synth_my_ip"]["filesets"] == ["rtl"]
    assert "constraints" not in core["filesets"]
    assert not (tmp_path / "constraints/my_ip.sdc").exists()
    assert (tmp_path / "constraints/my_ip.xdc").is_file()
    assert core_schema_errors(tmp_path / "my_ip.core") == []
    assert "synth_my_ip" in {h.name for h in TargetCatalog.build(tmp_path).list()}
    resolved = _resolve(tmp_path, "synth_my_ip", tmp_path / "build")
    assert resolved.eda_tool == "yosys"


def test_scaffold_physical_default_and_disabled_synthesis(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    for mode in (None, "physical"):
        files = scaffold_files(gather_scaffold_choices(_args(synth_mode=mode), ctx))
        core = _core(files)
        assert core["targets"]["synth_my_ip"]["flow_options"]["synth_mode"] == "physical"
        assert "constraints" in core["targets"]["synth_my_ip"]["filesets"]
        assert "constraints/my_ip.sdc" in files
    files = scaffold_files(gather_scaffold_choices(_args(asic=False), ctx))
    assert "synth_my_ip" not in _core(files)["targets"]
    assert "constraints" not in _core(files)["filesets"]
    assert "constraints/my_ip.sdc" not in files
    assert tomllib.loads(files[".booley/booley.toml"])["flows"]["synth"] == {"enabled": False}


def test_scaffold_rejects_explicit_mode_with_no_asic(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path, monkeypatch)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for mode in ("logical", "physical"):
        args = _args(asic=False, synth_mode=mode)
        assert gather_scaffold_choices(args, ctx) is None, (
            "explicit synthesis mode with no ASIC was accepted"
        )
        assert not step_scaffold(ctx, args)
        assert ctx.results[-1].status == "err"
        assert {
            p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
        } == before
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

    # Private: the Synthesis Flow runs Yosys inside the Sandbox. `_configure_synth`
    # and `_execute_boundary` are its two Sandbox command calls; the real
    # `configure` parser still reads the argv the scaffolded Target produces.
    # `_synth_build_dir` and `_run` are the Flow's own build directory and run
    # entry, which the public CLI reaches only through that Sandbox.
    monkeypatch.setattr(AsicSynthesizeFlow, "_configure_synth", configure_actual)
    monkeypatch.setattr(
        AsicSynthesizeFlow,
        "_execute_boundary",
        lambda *a, **k: SubprocessResult(
            returncode=0,
            stdout=(
                "Chip area for top module 'my_ip': 1000.0\nNumber of cells: 10\n"
                "STA_WORST_SLACK_NS: 0.500000\nOPENROAD_DESIGN_AREA_UM2: 1000.0\n"
                "STA_PERCLOCK: name=clk period_ns=10.0 wns_ns=0.5 whs_ns=0.1 setup_same_clock=1\n"
            ),
            stderr="",
        ),
    )
    flow = AsicSynthesizeFlow()
    flow.parse_args(
        [
            "--target",
            "synth_my_ip",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    result = flow._run()
    path = write_report(flow, result)
    report = json.loads(path.read_text())
    assert report["passed"] is True
    assert report["detail"]["synth_my_ip"]["synth_mode"] == "logical", (
        "scaffold request did not reach the delivered synthesis report"
    )
    assert path.parent.name.isdigit()
    assert report["target"] == "synth_my_ip"
    assert report["eda_tool"] == "yosys"
    assert report["detail"]["synth_my_ip"]["area_source"] == "yosys_mapped"
    assert commands[0][commands[0].index("--synth-mode") + 1] == "logical"


# ---------------------------------------------------------------------------
# --stealth: Booley-authored cores live under .booley/cores/ (issue #2332)
# ---------------------------------------------------------------------------


def _stealth_scaffold(root: Path, monkeypatch) -> None:
    _git(root, "init", "-q")
    ctx = _ctx(root, monkeypatch)
    assert step_scaffold(ctx, _args(stealth=True)) is True
    _initialize(root, ctx)


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
    _stealth_scaffold(tmp_path, monkeypatch)
    booley_dir = tmp_path / ".booley"
    _git(tmp_path, "add", "-A")
    _git(booley_dir, "add", "-A")
    _git(booley_dir, "commit", "-qm", "stealth files")

    for target in ("sim_my_ip", "lint_my_ip"):
        _resolve(tmp_path, target, tmp_path / "build" / target)
    bad = _resolve(tmp_path, "lint_selftest_bad", tmp_path / "build" / "bad", doctor_private=True)
    assert bad.vlnv == "::my_ip_booley_doctor_selftest:0"
    assert bad.rtl_source_files[0].absolute(bad.build_root).is_file()
    with monkeypatch.context() as patch:
        patch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        assert TargetCatalog.build(tmp_path).select("lint_selftest_bad").doctor_private
    sim = _resolve(tmp_path, "sim_my_ip", tmp_path / "build" / "sim2")
    assert selftest_overlay.stage_bad_overlay(booley_dir, "sim", sim.build_root) == 1

    # The whole Doctor run includes the core audit and the stealth-cores check.
    _doctor_findings(tmp_path, monkeypatch)
    capsys.readouterr()
