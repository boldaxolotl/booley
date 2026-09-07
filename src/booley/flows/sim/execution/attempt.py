"""One authenticated adapter process attempt shared by Simulation consumers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
)

from .freshness import ArtifactValidationError, snapshot_artifact, validate_fresh_artifact

ProcessInvoker = Callable[..., SubprocessResult]


@dataclass(frozen=True)
class AdapterAttemptRequest:
    """Fully rendered process and authenticated result contract for one attempt."""

    command: tuple[str, ...]
    timeout_s: int
    identity: AdapterTransportIdentity
    result_root: Path


@dataclass(frozen=True)
class AdapterAttemptOutcome:
    """Process truth plus an authenticated adapter result or validation error."""

    process: SubprocessResult
    result: AdapterResult | None
    error: str | None


def execute_adapter_attempt(
    invoke: ProcessInvoker, request: AdapterAttemptRequest
) -> AdapterAttemptOutcome:
    """Run and authenticate exactly one adapter process attempt."""
    terminal_before = snapshot_artifact(request.identity.result_path)
    partial = partial_result_identity(request.identity)
    partial_before = snapshot_artifact(partial.result_path)
    process = invoke(list(request.command), timeout=request.timeout_s)
    identity = request.identity
    before = terminal_before
    if not identity.result_path.exists() and process.timed_out:
        identity = partial
        before = partial_before
        if not identity.result_path.exists():
            return AdapterAttemptOutcome(process, None, None)
    try:
        validate_fresh_artifact(
            identity.result_path,
            roots=(request.result_root,),
            before=before,
        )
        result = read_adapter_result(identity)
    except (AdapterTransportError, ArtifactValidationError, OSError) as exc:
        return AdapterAttemptOutcome(process, None, str(exc))
    return AdapterAttemptOutcome(process, result, None)


__all__ = [
    "AdapterAttemptOutcome",
    "AdapterAttemptRequest",
    "ProcessInvoker",
    "execute_adapter_attempt",
]
