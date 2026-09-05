"""Private selection boundary for Simulation adapters."""

from __future__ import annotations

from collections.abc import Callable

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.backends.cocotb import prepare_invocation as prepare_cocotb
from booley.flows.sim.backends.icarus import prepare_invocation as prepare_icarus
from booley.flows.sim.backends.verilator import prepare_invocation as prepare_verilator

AdapterPreparer = Callable[[PreparedSimulationWork], list[str]]


class UnsupportedSimulationAdapterError(ValueError):
    """The resolved Target names no supported Simulation adapter."""


_PREPARERS: dict[str, AdapterPreparer] = {
    "cocotb": prepare_cocotb,
    "icarus": prepare_icarus,
    "verilator": prepare_verilator,
}


def prepare_adapter_invocation(work: PreparedSimulationWork) -> list[str]:
    """Render *work* through its selected adapter."""
    try:
        prepare = _PREPARERS[work.adapter]
    except KeyError as exc:  # pragma: no cover - Literal protects typed callers
        raise UnsupportedSimulationAdapterError(
            f"unsupported Simulation adapter: {work.adapter!r}"
        ) from exc
    return prepare(work)


__all__ = ["UnsupportedSimulationAdapterError", "prepare_adapter_invocation"]
