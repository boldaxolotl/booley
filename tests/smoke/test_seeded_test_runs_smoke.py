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

import re
import shutil
import textwrap
from pathlib import Path

import pytest
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

from booley.config.project_config import normalize_tests_toml
from booley.flows.sim.backends.cocotb import run_cocotb_sim
from booley.flows.sim.backends.icarus import run_icarus_image
from booley.flows.sim.backends.verilator import run_verilated_binary
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan
from booley.flows.test_runs import MAX_SEED

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
    result = run_bounded_child(command, cwd=cwd, env=env, timeout=SUBPROCESS_TIMEOUT_S)
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


@pytest.mark.parametrize(("template", "tag"), [(None, "NATIVE"), ("+seed={seed}", "TEMPLATE")])
def test_verilator_seed_decides_the_draws(verilator_binary: Path, template, tag) -> None:
    plan = _plan("verilator", **({"seed": template} if template else {}))

    def draw_at(seed: int) -> str:
        run = run_verilated_binary(
            top_module="tb", bin_dir=verilator_binary.parent, plusargs=_plusargs(plan, seed)
        )
        if template:
            assert _line(run.output, "TEMPLATE").split()[0] == str(seed)
        return _line(run.output, "NATIVE") + " | " + _line(run.output, "TEMPLATE")

    _assert_seed_decides(draw_at)


def test_icarus_seed_template_decides_the_testbench_draw(tmp_path: Path) -> None:
    _require("iverilog", "vvp")
    (tmp_path / "tb.sv").write_text(_HDL_BENCH, encoding="utf-8")
    _run(["iverilog", "-g2012", "-o", "tb", "tb.sv"], tmp_path)
    (tmp_path / "tb.scr").write_text("", encoding="utf-8")  # Edalize's image marker
    plan = _plan("icarus", seed="+seed={seed}")

    def draw_at(seed: int) -> str:
        run = run_icarus_image(build_dir=tmp_path, plusargs=_plusargs(plan, seed))
        template = _line(run.output, "TEMPLATE")
        assert template.split()[0] == str(seed)
        return template

    _assert_seed_decides(draw_at)


def test_cocotb_seed_decides_python_random(tmp_path: Path) -> None:
    _require("iverilog", "vvp", "cocotb-config")
    (tmp_path / "tb.sv").write_text("module tb; reg clk = 0; endmodule\n", encoding="utf-8")
    (tmp_path / "test_draw_module.py").write_text(_COCOTB_MODULE, encoding="utf-8")
    _run(["iverilog", "-g2012", "-s", "tb", "-o", "tb", "tb.sv"], tmp_path)
    (tmp_path / "tb.scr").write_text("", encoding="utf-8")  # Edalize's image marker

    def draw_at(seed: int) -> str:
        work = tmp_path / f"run-{seed}-{len(list(tmp_path.glob('run-*')))}"
        exit_code = run_cocotb_sim(
            build_dir=tmp_path,
            eda_tool="icarus",
            cocotb_module="test_draw_module",
            tests=["test_draw"],
            work_dir=work,
            seed=seed,
        )
        output = (work / "run.log").read_text(encoding="utf-8")
        assert exit_code == 0, output
        return _line(output, "DRAW")

    _assert_seed_decides(draw_at)
