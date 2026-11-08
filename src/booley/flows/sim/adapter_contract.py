"""Private work description shared by Simulation execution and its adapters."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from booley.core.boundary import (
    require_bool_value,
    require_dict,
    require_int,
    require_list,
    require_str_value,
)
from booley.flows.sim.result import format_infra_error
from booley.flows.test_runs import TestRun, test_run_document, test_run_from_document

AdapterKind = Literal["verilator", "icarus", "cocotb"]


@dataclass(frozen=True)
class PreparedSimulationWork:
    """Resolved facts needed by exactly one adapter invocation."""

    adapter: AdapterKind
    build_dir: str
    run_cwd: str
    timeout_s: int
    work_dir: str = ""
    eda_tool: str = ""
    max_rundir_bytes: int = 0
    plusargs: tuple[str, ...] = ()
    #: Environment of this launch only (a Test Variant's ``env``); never a build input.
    environment: tuple[tuple[str, str], ...] = ()
    #: Seed the Cocotb adapter exports through cocotb's own seed variable.
    cocotb_seed: int | None = None
    #: The concrete Test Runs this process executes, aligned with ``tests``
    #: (one unnamed run when ``tests`` is empty).
    test_runs: tuple[TestRun, ...] = ()
    #: Directory name of this process's evidence among its Target's: the Test
    #: Run name, or the batch label of a Cocotb batch.
    artifact_group: str = ""
    trace: bool = False
    trace_mode: str = "vcd_fifo"
    trace_scope: str = ""
    trace_args: tuple[str, ...] = ()
    trace_files: tuple[str, ...] = ()
    runtime_inputs: tuple[str, ...] = ()
    pass_sentinels: tuple[str, ...] = ()
    fail_sentinels: tuple[str, ...] = ()
    top: str = ""
    cocotb_module: str = ""
    tests: tuple[str, ...] = ()
    result_verbosity: str = "compact"
    sim_time_grace_s: float = 0.0
    adapter_result_path: str = ""
    attempt_token: str = ""
    target_identity: str = ""

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("Simulation adapter timeout must be positive")
        transport = (self.adapter_result_path, self.attempt_token, self.target_identity)
        if any(transport) and not all(transport):
            raise ValueError(
                "Simulation adapter transport requires a result path, attempt token, "
                "and Target identity"
            )


#: Version of the work document one adapter launch receives as its only argument.
SIMULATION_WORK_SCHEMA = 1


class SimulationWorkError(ValueError):
    """A prepared-work document an adapter cannot accept."""


def encode_simulation_work(work: PreparedSimulationWork) -> str:
    """The versioned JSON document that carries *work* to its adapter process.

    Every dataclass field is written, so a field added to the work description
    reaches the adapter without a second list to keep in step.
    """
    document: dict[str, object] = {
        field.name: getattr(work, field.name) for field in dataclasses.fields(work)
    }
    document["test_runs"] = [test_run_document(run) for run in work.test_runs]
    document["schema"] = SIMULATION_WORK_SCHEMA
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    return tuple(
        require_str_value(item, field=f"{name}[{index}]", allow_empty=True)
        for index, item in enumerate(require_list(value, field=name))
    )


def _environment_pairs(value: object, name: str) -> tuple[tuple[str, str], ...]:
    pairs = [_string_tuple(item, name) for item in require_list(value, field=name)]
    if any(len(pair) != 2 for pair in pairs):
        raise SimulationWorkError(f"{name} entries must be [name, value] pairs")
    return tuple((pair[0], pair[1]) for pair in pairs)


def _float(value: object, name: str) -> float:
    """A float field; ``inf`` is legal (``sim_time_grace_s = inf`` in project config)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SimulationWorkError(f"{name} must be a number, got {value!r}")
    return float(value)


def _test_runs(value: object, name: str) -> tuple[TestRun, ...]:
    return tuple(
        test_run_from_document(entry, trust_fingerprint=True)
        for entry in require_list(value, field=name)
    )


#: Decoder of each work field, keyed by the field's annotation text.
_FIELD_DECODERS: dict[str, Callable[[object, str], object]] = {
    "AdapterKind": lambda value, name: require_str_value(value, field=name),
    "str": lambda value, name: require_str_value(value, field=name, allow_empty=True),
    "int": lambda value, name: require_int(value, field=name),
    "float": _float,
    "bool": lambda value, name: require_bool_value(value, field=name),
    "int | None": lambda value, name: None if value is None else require_int(value, field=name),
    "tuple[str, ...]": _string_tuple,
    "tuple[tuple[str, str], ...]": _environment_pairs,
    "tuple[TestRun, ...]": _test_runs,
}


def decode_simulation_work(text: str) -> PreparedSimulationWork:
    """Rebuild the work a document describes, or raise :class:`SimulationWorkError`.

    The document must carry this version of the schema and exactly the fields
    of :class:`PreparedSimulationWork`. Values are not re-derived: a document
    that decodes is the work Booley encoded.
    """
    try:
        document = require_dict(json.loads(text), field="work document")
        schema = document.pop("schema", None)
        if schema != SIMULATION_WORK_SCHEMA:
            raise SimulationWorkError(
                f"work document schema {schema!r} is not supported "
                f"(this adapter reads schema {SIMULATION_WORK_SCHEMA})"
            )
        fields = {
            field.name: str(field.type) for field in dataclasses.fields(PreparedSimulationWork)
        }
        if unknown := sorted(set(document) - set(fields)):
            raise SimulationWorkError(f"work document has unknown fields: {', '.join(unknown)}")
        if missing := sorted(set(fields) - set(document)):
            raise SimulationWorkError(f"work document is missing fields: {', '.join(missing)}")
        values = {
            name: _FIELD_DECODERS[kind](document[name], name) for name, kind in fields.items()
        }
        return PreparedSimulationWork(**values)  # pyright: ignore[reportArgumentType]
    except ValueError as exc:
        raise SimulationWorkError(str(exc)) from exc


def run_adapter_entry(argv: list[str], run: Callable[[PreparedSimulationWork], int]) -> int:
    """The shared ``main`` of every adapter: decode the one argument, then *run* it.

    A document the adapter cannot decode is an infrastructure failure: the
    marker goes to stdout, no authenticated result is written, and the exit
    code is 2.
    """
    try:
        if len(argv) != 1:
            raise SimulationWorkError(
                f"expected exactly one work document argument, got {len(argv)}"
            )
        work = decode_simulation_work(argv[0])
    except SimulationWorkError as exc:
        print(format_infra_error(f"Simulation adapter work document rejected: {exc}"))
        return 2
    return run(work)


__all__ = [
    "SIMULATION_WORK_SCHEMA",
    "AdapterKind",
    "PreparedSimulationWork",
    "SimulationWorkError",
    "decode_simulation_work",
    "encode_simulation_work",
    "run_adapter_entry",
]
