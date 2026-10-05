"""One authenticated adapter process attempt shared by Simulation consumers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
)

from .freshness import (
    ArtifactStamp,
    ArtifactValidationError,
    snapshot_artifact,
    validate_fresh_artifact,
)

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
    error_kind: Literal["authentication", "runtime", "cleanup"] | None = None
    cleanup_error: str | None = None


def execute_adapter_attempt(
    invoke: ProcessInvoker, request: AdapterAttemptRequest
) -> AdapterAttemptOutcome:
    """Run and authenticate exactly one adapter process attempt."""
    from booley.flows.terminal_progress import announce_unit

    identity = request.identity
    announce_unit(
        f"simulation: {','.join(identity.selected_tests)} attempt={identity.attempt_token}",
        target=identity.target_identity,
    )
    terminal_before = snapshot_artifact(request.identity.result_path)
    partial = partial_result_identity(request.identity)
    partial_before = snapshot_artifact(partial.result_path)
    process = invoke(list(request.command), timeout=request.timeout_s)
    # Invokers return only after their owned child has been stopped and reaped.
    outcome = _authenticate_attempt(request, process, terminal_before, partial_before)
    if outcome.result is not None and not request.identity.result_path.exists():
        return outcome
    try:
        partial.result_path.unlink(missing_ok=True)
    except OSError as exc:
        diagnostic = f"could not remove adapter partial evidence: {exc}"
        outcome = replace(
            outcome,
            error=outcome.error or diagnostic,
            error_kind=outcome.error_kind or "cleanup",
            cleanup_error=diagnostic,
        )
    return outcome


def _authenticate_attempt(
    request: AdapterAttemptRequest,
    process: SubprocessResult,
    terminal_before: ArtifactStamp | None,
    partial_before: ArtifactStamp | None,
) -> AdapterAttemptOutcome:
    identity = request.identity
    before = terminal_before
    partial = partial_result_identity(identity)
    fallback = not identity.result_path.exists() and process.timed_out
    if fallback:
        identity = partial
        before = partial_before
    if not identity.result_path.exists():
        return AdapterAttemptOutcome(process, None, None, "runtime")
    try:
        validate_fresh_artifact(identity.result_path, roots=(request.result_root,), before=before)
        result = read_adapter_result(identity)
    except (AdapterTransportError, ArtifactValidationError) as exc:
        return AdapterAttemptOutcome(process, None, str(exc), "authentication")
    except OSError as exc:
        return AdapterAttemptOutcome(process, None, str(exc), "runtime")
    if fallback:
        try:
            partial.result_path.replace(request.identity.result_path)
        except OSError as exc:
            return AdapterAttemptOutcome(
                process, result, f"could not promote adapter partial evidence: {exc}", "cleanup"
            )
    return AdapterAttemptOutcome(process, result, None)


__all__ = [
    "AdapterAttemptOutcome",
    "AdapterAttemptRequest",
    "ProcessInvoker",
    "execute_adapter_attempt",
]
