"""Golden snapshots of what the ASIC Synthesis Flow plans and writes.

What drift these protect against: the ``ca5adaf`` class (the retired ABC-only
``--sdc`` flag crashed argparse deep inside the Sandbox) and any added,
dropped or reordered token or recipe line, which a substring assert cannot see
(see ``tests/golden/conftest.py`` for the ``BOOLEY_REGEN_GOLDEN`` convention).

Each test drives the Flow's CLI over a real FuseSoC core; only the ``make`` the
Flow launches is a fake on ``PATH``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from booley.flows.synth.backends import pipeline as syn_make
from booley.flows.synth.flow import AsicSynthesizeFlow
from tests.golden.conftest import assert_matches_golden, normalize_work_dir
from tests.path_tool_fake import PathToolFake

_CORE = """CAPI=2:
name: ::syn_demo:0
filesets:
  rtl:
    files:
      - rtl/include/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
  constraints:
    files:
      - constraints/dut.sdc: {file_type: SDC}
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]
parameters:
  WIDTH: {datatype: int, paramtype: vlogparam, default: 8}
  SYNTHESIS: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  lite:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx%s}
    filesets: [rtl, constraints, tb]
    parameters: [%s]
    toplevel: dut
"""


def _synth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipe: str,
    *argv: str,
    parameters: str = "WIDTH, SYNTHESIS",
):
    """Run the Flow's CLI on Target ``lite`` with extra *recipe* flow options and *parameters*."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")  # the in-process CLI runs only in the Sandbox
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    if "--dry-run" in argv:
        monkeypatch.delenv("PRJ_LIB_DIR", raising=False)  # planning needs no liberty
    else:
        # The liberty the Sandbox provides, so the configure half finds it on any host.
        liberty = tmp_path / "pdk" / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"
        liberty.parent.mkdir(parents=True)
        liberty.write_text("library(cells) {}\n", encoding="utf-8")
        monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path / "pdk"))
    # The Makefile carries the run's random attempt token.
    monkeypatch.setattr(syn_make, "new_attempt_token", lambda: "0123456789abcdef" * 2)
    # FuseSoC, a declared dependency, runs from the test interpreter's environment.
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(Path(sys.executable).parent), os.environ["PATH"]))
    )
    PathToolFake.install(tmp_path / "bin", "make", monkeypatch).answer("-C")
    (tmp_path / "golden.core").write_text(_CORE % (recipe, parameters), encoding="utf-8")
    (tmp_path / ".booley").mkdir()
    for name in ("rtl/include/defs.svh", "rtl/pkg.sv", "rtl/dut.sv", "tb/tb_dut.sv"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("// golden fixture\n", encoding="utf-8")
    (tmp_path / "constraints").mkdir()
    (tmp_path / "constraints" / "dut.sdc").write_text(
        "create_clock -name clk -period 4.0 [get_ports clk]\n", encoding="utf-8"
    )
    return (
        AsicSynthesizeFlow()
        .execute_cli(["--target", "lite", "--project", str(tmp_path), *argv])
        .outcome
    )


@pytest.mark.parametrize(
    ("golden_name", "recipe"),
    [
        # Physical: the Target SDC is forwarded with --sta-sdc; no bare --sdc (ca5adaf).
        ("argv_bare.txt", ""),
        ("argv_flatten_logical.txt", ", flatten: true, synth_mode: logical"),
    ],
)
def test_synth_planned_commands_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_name: str, recipe: str
) -> None:
    """Snapshot every command the dry-run plans: the configure argv and its make stages."""
    outcome = _synth(tmp_path, monkeypatch, recipe, "--dry-run")
    assert outcome.exit_code == 0, outcome.report_text
    commands = outcome.detail["work_units"][0]["commands"]
    text = "\n\n".join("\n".join(command["argv"]) for command in commands)
    assert_matches_golden(f"asic_synthesize/{golden_name}", text)


@pytest.mark.parametrize(
    ("file_name", "golden_name", "recipe", "argv"),
    [
        # A dropped stage, a lost BOOLEY_STAGE marker or a Booley/python recipe dependency.
        ("Makefile", "makefile_logical.txt", "", ()),
        # sv2v frontend, `synth -flatten`, chparam, balanced ABC, netlist/stat emission.
        ("synth.ys", "synth_ys_logical.txt", "", ()),
        # Unflattened `synth`, no parameters to set, the bare liberty mapping.
        ("synth.ys", "synth_ys_bare.txt", ", flatten: false", ("--abc-recipe", "default")),
        # read_slang with -I/-D/-G and chformal instead of sv2v and chparam.
        ("synth.ys", "synth_ys_slang.txt", ", frontend: slang", ()),
        # read_slang with no define or parameter to pass.
        ("synth.ys", "synth_ys_slang_bare.txt", ", frontend: slang, flatten: false", ()),
    ],
)
def test_synth_configured_build_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    golden_name: str,
    recipe: str,
    argv: tuple[str, ...],
) -> None:
    """Snapshot what the configure half writes for the ``make -C`` it launches (logical)."""
    parameters = "" if golden_name.endswith("_bare.txt") else "WIDTH, SYNTHESIS"
    text = _configured(tmp_path, monkeypatch, file_name, recipe, *argv, parameters=parameters)
    assert_matches_golden(f"asic_synthesize/{golden_name}", text)


@pytest.mark.parametrize(
    ("golden_name", "argv"),
    [
        ("abc_default.txt", ("--abc-recipe", "default")),
        ("abc_fast.txt", ("--abc-recipe", "fast")),
        ("abc_balanced.txt", ("--abc-recipe", "balanced")),
        # A custom script is forwarded verbatim to -script.
        ("abc_custom_plus.txt", ("--abc-script", "+strash;dch;map")),
        # With a delay target, every built-in recipe maps with it: "default"
        # through -D, the scripted recipes through the ``{D}`` placeholder
        # that Yosys expands to ``-D <ps>``.
        ("abc_default_delay.txt", ("--abc-recipe", "default", "--abc-delay-ps", "3333")),
        ("abc_fast_delay.txt", ("--abc-recipe", "fast", "--abc-delay-ps", "3333")),
        ("abc_balanced_delay.txt", ("--abc-recipe", "balanced", "--abc-delay-ps", "3333")),
    ],
)
def test_abc_command_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_name: str, argv: tuple[str, ...]
) -> None:
    """Snapshot each ABC technology-mapping line, with and without a delay target."""
    script = _configured(tmp_path, monkeypatch, "synth.ys", "", *argv)
    [abc] = [line for line in script.splitlines() if " abc " in line]
    assert_matches_golden(f"yosys/{golden_name}", abc)


def _configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    recipe: str,
    *argv: str,
    parameters: str = "WIDTH, SYNTHESIS",
) -> str:
    """Run a logical synthesis and return *file_name* from the configured build dir."""
    _synth(tmp_path, monkeypatch, f", synth_mode: logical{recipe}", *argv, parameters=parameters)
    build_dir = tmp_path / ".booley" / ".runtime" / "edalize" / "synth" / "lite" / "synth"
    return normalize_work_dir((build_dir / file_name).read_text(encoding="utf-8"), tmp_path)
