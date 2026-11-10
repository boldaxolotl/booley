"""Private selection boundary for Simulation adapters."""

from __future__ import annotations

from booley.flows.sim.adapter_contract import PreparedSimulationWork, encode_simulation_work


class UnsupportedSimulationAdapterError(ValueError):
    """The resolved Target names no supported Simulation adapter."""


_ADAPTER_MODULES: dict[str, str] = {
    "cocotb": "booley.flows.sim.backends.cocotb",
    "icarus": "booley.flows.sim.backends.icarus",
    "verilator": "booley.flows.sim.backends.verilator",
}


def prepare_adapter_invocation(
    work: PreparedSimulationWork, *, python: str = "python3"
) -> list[str]:
    """Render the command that launches *work* in its adapter: one document argument."""
    try:
        module = _ADAPTER_MODULES[work.adapter]
    except KeyError as exc:  # pragma: no cover - Literal protects typed callers
        raise UnsupportedSimulationAdapterError(
            f"unsupported Simulation adapter: {work.adapter!r}"
        ) from exc
    return [python, "-m", module, encode_simulation_work(work)]


__all__ = ["UnsupportedSimulationAdapterError", "prepare_adapter_invocation"]
