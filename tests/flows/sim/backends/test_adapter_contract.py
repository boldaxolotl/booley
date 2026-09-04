"""Parent-side Simulation adapter composition contract."""

import ast
from pathlib import Path

import pytest

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.execution.composition import prepare_adapter_invocation


def test_native_adapter_invocations_preserve_order_and_option_shaping() -> None:
    common = {
        "build_dir": "build/sim",
        "run_cwd": "assets",
        "timeout_s": 12,
        "max_rundir_bytes": 2048,
        "plusargs": ("test_id=2", "--firmware=image.elf"),
        "trace": True,
        "trace_files": ("dump.vcd",),
        "pass_sentinels": ("PASS",),
        "fail_sentinels": ("FAIL",),
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
    assert icarus[:3] == ["python3", "-m", "booley.flows.sim.backends.icarus"]
    assert "--top" not in icarus
    assert "--plusarg=--firmware=image.elf" in icarus


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
        )
    )

    assert command[:3] == ["python3", "-m", "booley.flows.sim.backends.cocotb"]
    assert not any(argument.startswith("--test=") for argument in command)
    assert command[command.index("--eda-tool") + 1] == "icarus"


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
