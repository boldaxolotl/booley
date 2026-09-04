"""Shared attempt precedence for Simulation execution."""

from __future__ import annotations

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    read_adapter_result,
)
from booley.flows.sim.build import BuildOutcome


class AdapterEvidenceError(RuntimeError):
    """A normally completed adapter supplied no trustworthy terminal result."""


def read_completed_adapter_result(
    identity: AdapterTransportIdentity,
    process: SubprocessResult,
    build: BuildOutcome,
) -> AdapterResult | None:
    """Apply build/wrapper/transport precedence and decode terminal evidence.

    A design-rejected build never starts an adapter. A wrapper timeout may kill
    the child before terminal publication and therefore keeps timeout
    precedence. Synthetic invocations with no dispatch timestamp are accepted
    for compatibility tests that inject historical marker-only fixtures.
    """
    if build.design_failed:
        return None
    if not identity.result_path.exists():
        if process.timed_out or process.dispatched_unix <= 0:
            return None
        raise AdapterEvidenceError(
            "adapter completed without authenticated terminal result evidence"
        )
    try:
        result = read_adapter_result(identity)
    except AdapterTransportError as exc:
        raise AdapterEvidenceError(f"invalid authenticated adapter result: {exc}") from exc
    if result.passed and (process.returncode != 0 or not build.passed):
        raise AdapterEvidenceError(
            "authenticated adapter pass contradicts process or build evidence"
        )
    if result.failure_kind == "infrastructure":
        raise AdapterEvidenceError(result.detail or "Simulation adapter infrastructure failure")
    return result


__all__ = ["AdapterEvidenceError", "read_completed_adapter_result"]
