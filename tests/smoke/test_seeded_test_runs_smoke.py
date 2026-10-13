"""Real-simulator proof of every seed mechanism (ADR 0072).

For each mechanism Booley uses, the same seed must repeat the random values
and a different seed must change them, at both ends of the seed range. Each
test launches the simulator with exactly the arguments and environment
Booley's own plan and adapter build, so it proves the mapping, not just the
simulator.

These run where the simulators exist (the Booley Sandbox Image); elsewhere
they skip with the missing tool named. The always-on tests of the command
line and environment live in ``tests/flows/sim/test_test_run_launch.py`` and
``tests/flows/sim/backends``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.sim.backends import cocotb as cocotb_backend
from booley.flows.sim.backends import icarus as icarus_backend
from booley.flows.sim.backends import verilator as verilator_backend
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan
from booley.flows.test_runs import MAX_SEED

_TOOL_TIMEOUT_S = 180
_OTHER_SEED = 271828

# Two draws from the simulator's own generator, and the seed argument the
# testbench was given (0 when the Project template passed none).
_HDL_BENCH = textwrap.dedent(
    """\
    module tb;
      integer given;
      integer state;
      initial begin
        if (!$value$plusargs("seed=%d", given)) given = 0;
        state = given;
        $display("NATIVE %0d %0d", $urandom, $urandom);
        $display("TEMPLATE %0d %0d", given, $random(state));
        $finish;
      end
    endmodule
    """
)

_COCOTB_MODULE = textwrap.dedent(
    """\
    import random

    import cocotb


    @cocotb.test()
    async def test_draw(dut):
        cocotb.log.info("DRAW %d %d", random.getrandbits(64), random.getrandbits(64))
    """
)


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"real seed proof needs {', '.join(missing)} on PATH (Booley Sandbox Image)")


def _plan(adapter: str, **keys: object) -> TargetTestPlan:
    section = normalize_tests_toml({"sim": {"tests": ["draw"], **keys}})["sim"]
    return build_target_test_plan("sim", section, adapter=adapter)


def _plusargs(plan: TargetTestPlan, seed: int) -> list[str]:
    """The plusargs the engine hands the adapter for the Test Run ``draw@seed``."""
    launch = plan.launch((plan.run("draw", seed=seed),))
    return [argument.removeprefix("+") for argument in launch.arguments]


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        timeout=_TOOL_TIMEOUT_S,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _line(output: str, tag: str) -> str:
    matches = re.findall(rf"{tag} (.*)", output)
    assert len(matches) == 1, output
    return matches[0].strip()


def _assert_seed_decides(draw_at) -> None:
    """Same seed repeats; another seed differs; both ends of the range work."""
    first = draw_at(1)
    assert draw_at(1) == first
    assert draw_at(_OTHER_SEED) != first
    top = draw_at(MAX_SEED)
    assert draw_at(MAX_SEED) == top
    assert top != first


@pytest.fixture(scope="module")
def verilator_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require("verilator")
    work = tmp_path_factory.mktemp("verilator-seed")
    (work / "tb.sv").write_text(_HDL_BENCH, encoding="utf-8")
    _run(
        ["verilator", "--binary", "--timing", "-Wno-fatal", "--Mdir", "obj", "-o", "Vtb", "tb.sv"],
        work,
    )
    return work / "obj" / "Vtb"


def test_verilator_native_seed_decides_urandom(verilator_binary: Path) -> None:
    plan = _plan("verilator")

    def draw_at(seed: int) -> str:
        command, env = verilator_backend._build_run_cmd(
            verilator_binary, verilator_binary.parent, _plusargs(plan, seed)
        )
        assert f"+verilator+seed+{seed}" in command
        return _line(_run(command, verilator_binary.parent, env), "NATIVE")

    _assert_seed_decides(draw_at)


def test_verilator_target_with_template_gets_both_from_one_seed(verilator_binary: Path) -> None:
    plan = _plan("verilator", seed="+seed={seed}")

    def draw_at(seed: int) -> str:
        command, env = verilator_backend._build_run_cmd(
            verilator_binary, verilator_binary.parent, _plusargs(plan, seed)
        )
        output = _run(command, verilator_binary.parent, env)
        template = _line(output, "TEMPLATE")
        assert template.split()[0] == str(seed), "the testbench must read the same seed"
        return _line(output, "NATIVE") + " | " + template

    _assert_seed_decides(draw_at)


def test_icarus_seed_template_decides_the_testbench_draw(tmp_path: Path) -> None:
    _require("iverilog", "vvp")
    (tmp_path / "tb.sv").write_text(_HDL_BENCH, encoding="utf-8")
    _run(["iverilog", "-g2012", "-o", "tb.vvp", "tb.sv"], tmp_path)
    plan = _plan("icarus", seed="+seed={seed}")

    def draw_at(seed: int) -> str:
        command = icarus_backend._build_vvp_cmd(
            icarus_backend._find_vvp(), tmp_path, "tb.vvp", _plusargs(plan, seed)
        )
        template = _line(_run(command, tmp_path), "TEMPLATE")
        assert template.split()[0] == str(seed)
        return template

    _assert_seed_decides(draw_at)


def test_icarus_without_a_template_is_unseeded_and_gets_no_seed_argument() -> None:
    plan = _plan("icarus")
    run = plan.run("draw")
    assert run.name == "draw"
    assert plan.launch((run,)).arguments == ("+test_id=0",)


def test_cocotb_seed_variable_decides_python_random(tmp_path: Path) -> None:
    _require("iverilog", "vvp", "cocotb-config")
    version = cocotb_backend._probe_cocotb_version()
    assert version is not None, "cocotb-config --version must report the installed version"
    (tmp_path / "tb.sv").write_text("module tb; reg clk = 0; endmodule\n", encoding="utf-8")
    (tmp_path / "test_draw_module.py").write_text(_COCOTB_MODULE, encoding="utf-8")
    _run(["iverilog", "-g2012", "-s", "tb", "-o", "tb", "tb.sv"], tmp_path)
    # The adapter finds the image through its Edalize ``.scr`` sibling.
    (tmp_path / "tb.scr").write_text("", encoding="utf-8")
    command = cocotb_backend._build_run_cmd(
        eda_tool="icarus", build_dir=tmp_path, plusargs=[], vcd=False
    )
    assert command is not None
    expected_variable = "RANDOM_SEED" if version[0] < 2 else "COCOTB_RANDOM_SEED"

    def draw_at(seed: int) -> str:
        results = tmp_path / f"results-{seed}.xml"
        env = cocotb_backend._build_cocotb_env(
            tmp_path, "test_draw_module", ["test_draw"], results, seed
        )
        assert env[expected_variable] == str(seed)
        env.setdefault("PATH", os.environ.get("PATH", ""))
        return _line(_run(command, tmp_path, env), "DRAW")

    _assert_seed_decides(draw_at)
