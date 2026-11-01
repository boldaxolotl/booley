"""Parent-side Simulation adapter composition contract."""

import ast
import dataclasses
import importlib
from pathlib import Path

import pytest

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.execution.composition import prepare_adapter_invocation


def test_native_adapter_invocations_preserve_order_and_option_shaping() -> None:
    common = {
        "build_dir": "build/sim",
        "run_cwd": "assets",
        "work_dir": "attempt/evidence",
        "timeout_s": 12,
        "max_rundir_bytes": 2048,
        "plusargs": ("test_id=2", "--firmware=image.elf"),
        "trace": True,
        "trace_files": ("dump.vcd",),
        "pass_sentinels": ("PASS",),
        "fail_sentinels": ("FAIL",),
        "runtime_inputs": ("firmware.hex", "vectors/input.bin"),
    }

    verilator = prepare_adapter_invocation(
        PreparedSimulationWork(
            adapter="verilator",
            top="tb",
            trace_mode="native_fst",
            trace_args=("--trace={file}",),
            **common,
        )
    )
    icarus = prepare_adapter_invocation(PreparedSimulationWork(adapter="icarus", **common))

    assert verilator[:3] == ["python3", "-m", "booley.flows.sim.backends.verilator"]
    assert "--trace-mode" in verilator
    assert "--trace-arg=--trace={file}" in verilator
    assert verilator[verilator.index("--work-dir") + 1] == "attempt/evidence"
    assert icarus[:3] == ["python3", "-m", "booley.flows.sim.backends.icarus"]
    assert "--top" not in icarus
    assert "--plusarg=--firmware=image.elf" in icarus
    assert icarus[icarus.index("--work-dir") + 1] == "attempt/evidence"
    assert "--runtime-input=firmware.hex" in verilator
    assert "--runtime-input=vectors/input.bin" in icarus


def test_cocotb_adapter_supports_unfiltered_batch() -> None:
    command = prepare_adapter_invocation(
        PreparedSimulationWork(
            adapter="cocotb",
            build_dir="build/sim",
            run_cwd=".",
            timeout_s=20,
            eda_tool="icarus",
            cocotb_module="tests.counter",
            tests=(),
            result_verbosity="full",
            sim_time_grace_s=4.5,
            runtime_inputs=("firmware.hex",),
        )
    )

    assert command[:3] == ["python3", "-m", "booley.flows.sim.backends.cocotb"]
    assert not any(argument.startswith("--test=") for argument in command)
    assert command[command.index("--eda-tool") + 1] == "icarus"
    assert "--runtime-input=firmware.hex" in command


def test_cocotb_adapter_receives_the_seed_as_its_own_option() -> None:
    work = PreparedSimulationWork(
        adapter="cocotb",
        build_dir="build/sim",
        run_cwd=".",
        timeout_s=20,
        eda_tool="icarus",
        cocotb_module="tests.counter",
        tests=("test_a",),
        cocotb_seed=2**31 - 1,
    )
    command = prepare_adapter_invocation(work)
    assert command[command.index("--seed") + 1] == "2147483647"

    unseeded = prepare_adapter_invocation(
        PreparedSimulationWork(
            adapter="cocotb",
            build_dir="build/sim",
            run_cwd=".",
            timeout_s=20,
            eda_tool="icarus",
            cocotb_module="tests.counter",
        )
    )
    assert "--seed" not in unseeded


@pytest.mark.parametrize("adapter", ["icarus", "verilator", "cocotb"])
def test_every_optional_field_renders_a_command_its_entry_point_accepts(adapter: str) -> None:
    every_field = {
        "build_dir": "build/sim",
        "run_cwd": "assets",
        "timeout_s": 12,
        "work_dir": "attempt/evidence",
        "eda_tool": "icarus",
        "max_rundir_bytes": 2048,
        "plusargs": ("test_id=2",),
        "environment": (("K", "V"),),
        "cocotb_seed": 7,
        "artifact_group": "g",
        "trace": True,
        "trace_mode": "native_fst",
        "trace_scope": "tb",
        "trace_args": ("--trace={file}",),
        "trace_files": ("dump.vcd",),
        "runtime_inputs": ("firmware.hex",),
        "pass_sentinels": ("PASS",),
        "fail_sentinels": ("FAIL",),
        "top": "tb",
        "cocotb_module": "tests.counter",
        "tests": ("test_a",),
        "result_verbosity": "full",
        "sim_time_grace_s": 1.5,
        "adapter_result_path": "result.json",
        "attempt_token": "token",
        "target_identity": "target",
    }
    # A new field must be added above so this guard keeps covering every flag.
    unrendered = {"adapter", "test_runs"}  # test_runs is structured; no writer renders it
    assert (
        set(every_field)
        == {f.name for f in dataclasses.fields(PreparedSimulationWork)} - unrendered
    )

    argv = prepare_adapter_invocation(PreparedSimulationWork(adapter=adapter, **every_field))

    entry_point = importlib.import_module(argv[2])
    entry_point._parse_args(argv[3:])  # argparse exits 2 on a flag the entry point rejects
    if adapter == "icarus":
        assert "--trace" in argv


def test_transport_identity_is_all_or_nothing() -> None:
    with pytest.raises(ValueError, match="transport requires"):
        PreparedSimulationWork(
            adapter="icarus",
            build_dir="build/sim",
            run_cwd=".",
            timeout_s=20,
            adapter_result_path="result.json",
        )


def test_leaf_contracts_do_not_import_flow_or_execution_engine() -> None:
    package = Path(__file__).parents[4] / "src" / "booley" / "flows" / "sim"
    forbidden = {
        "booley.flows.sim.flow",
        "booley.flows.sim.execution.engine",
        "booley.flows.sim.execution.composition",
    }
    for relative in ("adapter_contract.py", "adapter_transport.py"):
        tree = ast.parse((package / relative).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert forbidden.isdisjoint(imported)
