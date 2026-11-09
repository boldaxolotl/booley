"""Parent-side Simulation adapter composition contract."""

import ast
import base64
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.execution.composition import prepare_adapter_invocation
from booley.flows.test_runs import build_test_run


def _work(adapter: str = "verilator", **fields: Any) -> PreparedSimulationWork:
    return PreparedSimulationWork(
        adapter=adapter,  # type: ignore[arg-type]
        build_dir="build/sim",
        run_cwd="assets",
        timeout_s=12,
        **fields,
    )


def test_every_adapter_launch_is_one_work_document() -> None:
    from booley.flows.sim.adapter_contract import encode_simulation_work

    for adapter, module in (
        ("verilator", "booley.flows.sim.backends.verilator"),
        ("icarus", "booley.flows.sim.backends.icarus"),
        ("cocotb", "booley.flows.sim.backends.cocotb"),
    ):
        work = _work(adapter, plusargs=("--firmware=image.elf",), trace=True)
        document = encode_simulation_work(work)

        assert prepare_adapter_invocation(work) == ["python3", "-m", module, document]
        assert re.fullmatch(r"[\w-]+", document)  # nothing a shell or Git Bash would rewrite
        assert prepare_adapter_invocation(work, python="/venv/python")[0] == "/venv/python"


def test_work_document_round_trips_every_field() -> None:
    from booley.flows.sim.adapter_contract import decode_simulation_work, encode_simulation_work

    plain = build_test_run("test_a")
    varied = build_test_run(
        "test_b",
        variant="slow",
        seed=7,
        args=("+slow",),
        env={"K": "V"},
    )
    given: dict[str, Any] = {
        "work_dir": "attempt/evidence",
        "eda_tool": "icarus",
        "max_rundir_bytes": 2048,
        "plusargs": ("test_id=2", "--firmware=a b.elf"),
        "environment": (("K", "V"), ("K", "again"), ("EMPTY", "")),
        "cocotb_seed": 7,
        "test_runs": (plain, varied),
        "artifact_group": "g",
        "trace": True,
        "trace_mode": "native_fst",
        "trace_scope": "tb",
        "trace_args": ("--trace={file}",),
        "trace_files": ("dump.vcd",),
        "trace_convert_timeout_s": 90,
        "runtime_inputs": ("firmware.hex",),
        "pass_sentinels": ("PASS",),
        "fail_sentinels": ("FAIL",),
        "top": "tb",
        "cocotb_module": "tests.counter",
        "tests": ("test_a", "test_b"),
        "result_verbosity": "full",
        "sim_time_grace_s": float("inf"),  # project config may say inf
        "adapter_result_path": "result.json",
        "attempt_token": "token",
        "target_identity": "target",
    }
    every_field = {f.name for f in dataclasses.fields(PreparedSimulationWork)}
    # A field added to the work description must be given a value here, or it is not proven.
    assert set(given) | {"adapter", "build_dir", "run_cwd", "timeout_s"} == every_field

    work = _work("cocotb", **given)

    assert decode_simulation_work(encode_simulation_work(work)) == work
    assert decode_simulation_work(encode_simulation_work(_work())) == _work()


def _armor(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _document(**overrides: Any) -> str:
    from booley.flows.sim.adapter_contract import encode_simulation_work

    document = json.loads(base64.urlsafe_b64decode(encode_simulation_work(_work()) + "=="))
    for name, value in overrides.items():
        if value is None:
            document.pop(name)
        else:
            document[name] = value
    return _armor(json.dumps(document))


def test_work_document_rejects_wrong_schema_and_field_set() -> None:
    from booley.flows.sim.adapter_contract import SimulationWorkError, decode_simulation_work

    rejected = {
        "schema 2 is not supported": _document(schema=2),
        "unknown fields: surprise": _document(surprise=1),
        "missing fields: top": _document(top=None),
        "plusargs": _document(plusargs="not-a-list"),
        "environment entries must be": _document(environment=[["only-a-name"]]),
        "timeout_s": _document(timeout_s=True),
        "sim_time_grace_s must be a number": _document(sim_time_grace_s="1"),
        "must be a mapping": _armor("[]"),
        "Expecting value": _armor("not json"),
        "Invalid base64": "A",
        "timeout must be positive": _document(timeout_s=0),
        "Test Run record": _document(test_runs=[{"name": "x"}]),
    }
    for message, text in rejected.items():
        with pytest.raises(SimulationWorkError, match=message):
            decode_simulation_work(text)


def test_undecodable_document_is_an_infrastructure_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.flows.sim.backends import cocotb, icarus, verilator

    for main in (verilator.main, icarus.main, cocotb.main):
        for argv in (["not a document"], [], [_document(), "extra"]):
            assert main(argv) == 2
            out = capsys.readouterr().out
            assert out.startswith("[SIM_INFRA_ERROR] Simulation adapter work document rejected")


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
