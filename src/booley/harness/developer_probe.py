"""Doctor's developer memory probe (ADR 0028 Decision 12).

``booley doctor --deep`` spawns the cheapest real agent call (the internal
model, one turn, no Booley MCP server and no tools), reports the child
process's peak RSS, and so proves the provider is reachable and authorized.

The probe is fail-soft by contract: no agent backend, no auth, or a timeout
must degrade doctor to a SKIP, never crash it — every failure mode surfaces as
:class:`ProbeError` for the caller to report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.runtime.agent_execution import configure_agent_call
from booley.runtime.owned_scratch import owned_tempdir

if TYPE_CHECKING:
    from booley.config.agent import AgentSettings
    from booley.core.models import AgentCallParams, AgentResult

try:
    import resource  # POSIX-only; no Windows equivalent for RUSAGE_CHILDREN
except ImportError:  # pragma: no cover — Windows host
    resource = None  # type: ignore[assignment]

# Opaque token passed to getrusage(); resolved here so an injected getrusage
# works even where the resource module is absent.
_RUSAGE_CHILDREN = resource.RUSAGE_CHILDREN if resource is not None else -1

# Public: Doctor quotes the budget in its start-of-check announcement.
PROBE_TIMEOUT_S = 300
# The cheapest real agent call: one trivial turn, no Booley MCP server, no tools.
_PROBE_PROMPT = "Health probe: reply with the single word OK and stop."


@dataclass(frozen=True)
class ProbeUsage:
    """Tokens and cost one probe agent call spent."""

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float

    @classmethod
    def from_result(cls, result: AgentResult) -> ProbeUsage:
        """Copy the spend fields off an agent result."""
        return cls(
            input_tokens=result.input_tokens,
            cached_tokens=result.cached_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        )


@dataclass(frozen=True)
class ProbeMeasurement:
    """A successful probe: peak child RSS, whether it is exact, and the spend."""

    peak_rss_bytes: int
    exact: bool
    usage: ProbeUsage


class ProbeError(RuntimeError):
    """The probe agent could not run or produced no measurable child RSS.

    ``agent_failure`` distinguishes "the AGENT CALL itself failed" (auth
    failure, dead backend — a real project defect: other agent calls
    would fail for the same reason) from environment limitations of the
    probe (no process accounting, no RSS reading). Doctor reports the former
    loud and degrades only the latter to a SKIP.

    ``agent_called`` is True once the agent call was entered (tokens may have
    been spent); ``usage`` is the known spend, or None when it is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_failure: bool = False,
        agent_called: bool = False,
        usage: ProbeUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.agent_failure = agent_failure
        self.agent_called = agent_called
        self.usage = usage


def measure_developer_rss(
    project_root: Path,
    *,
    timeout_s: int = PROBE_TIMEOUT_S,
    getrusage: Callable[[int], Any] | None = None,
) -> ProbeMeasurement:
    """Spawn a short probe agent; return a :class:`ProbeMeasurement`.

    Both agent backends run their CLI/SDK as a child process, so
    ``getrusage(RUSAGE_CHILDREN).ru_maxrss`` (KiB on Linux) captures the
    high-water mark once the child is reaped. When the probe child sets a new
    high-water mark (``after > before``) the reading is exact; when an earlier
    doctor child was larger, ``after`` is still a safe *upper bound* for the
    probe — conservative in the invariant's favor — and ``exact`` is False.
    ``getrusage`` is injectable so tests never depend on real process
    accounting.

    Raises :class:`ProbeError` on any failure (backend unresolvable, no auth,
    agent error, timeout, or no child RSS observed) — the caller reports a SKIP.
    """
    # A scratch cwd keeps the text-only Codex home (with its auth link) out of the Project.
    try:
        with owned_tempdir("booley-developer-probe-") as work:
            return _measure_in(project_root, work, timeout_s, getrusage)
    except OSError as exc:  # fail-soft by contract: an unusable temp dir becomes a doctor SKIP
        raise ProbeError(f"probe agent failed: {exc}") from exc


def _probe_params(
    settings: AgentSettings, model: str, work: Path, timeout_s: int, label: str
) -> AgentCallParams:
    """The cheapest real agent call: one text-only turn, no tools, no Booley MCP server."""
    from booley.core.models import AgentCallParams

    return AgentCallParams(
        prompt=_PROBE_PROMPT,
        model=model,
        agent_cli=settings.cli_for_model_id(model),
        # Only the gateway pin sets an effort; otherwise Codex's own default.
        reasoning_effort=settings.effort_pin,
        cwd=str(work),
        text_only=True,
        allowed_agent_capabilities=[],
        max_turns=1,
        timeout_seconds=timeout_s,
        label=label,
    )


def _measure_in(
    project_root: Path,
    work: Path,
    timeout_s: int,
    getrusage: Callable[[int], Any] | None,
) -> ProbeMeasurement:
    """Run the probe agent call in the *work* directory (see :func:`measure_developer_rss`)."""
    import asyncio

    from booley.runtime import agent as agent_mod
    from booley.runtime import agent_config as config_mod

    try:
        config_mod.load_backend_config(project_root)
        if getrusage is None:
            if resource is None:
                raise ProbeError(
                    "child-process accounting (resource.getrusage) is "
                    "unavailable on this platform; using the fallback estimate"
                )
            getrusage = resource.getrusage
        settings = config_mod.get_backend_config().settings
        params = _probe_params(
            settings, settings.internal_model(), work, timeout_s, "doctor-developer-probe"
        )
        before = getrusage(_RUSAGE_CHILDREN).ru_maxrss
    except Exception as exc:  # fail-soft by contract: environment issues become a doctor SKIP
        raise ProbeError(f"probe agent failed: {exc}") from exc
    try:
        result = asyncio.run(agent_mod.call_agent(configure_agent_call(params)))
    except Exception as exc:
        # The agent call itself died — auth failure, dead backend. This is not
        # a probe limitation: other agent calls would fail for the
        # same reason (2026-07-23: expired seeded OAuth creds crashed every run
        # while doctor stayed green because this path degraded to SKIP).
        # A hit usage cap is the one agent-side error that is NOT a project
        # defect — it stays a SKIP.
        from booley.runtime.agent_errors import UsageLimitError

        raise ProbeError(
            f"probe agent failed: {exc}",
            agent_failure=not isinstance(exc, UsageLimitError),
            agent_called=True,
        ) from exc
    usage = ProbeUsage.from_result(result)
    try:
        after = getrusage(_RUSAGE_CHILDREN).ru_maxrss
    except Exception as exc:  # fail-soft by contract
        raise ProbeError(f"probe agent failed: {exc}", agent_called=True, usage=usage) from exc

    if result.timed_out:
        raise ProbeError(
            f"probe agent timed out after {timeout_s}s", agent_called=True, usage=usage
        )
    if after <= 0:
        raise ProbeError(
            "no child RSS observed around the probe agent call",
            agent_called=True,
            usage=usage,
        )
    return ProbeMeasurement(peak_rss_bytes=int(after) * 1024, exact=after > before, usage=usage)


def probe_model_access(
    project_root: Path,
    model: str,
    *,
    booley_dir: Path | None = None,
    timeout_s: int = PROBE_TIMEOUT_S,
) -> ProbeUsage:
    """Ask the provider for one trivial turn on *model*; return the spend.

    A refusal surfaces as :class:`ProbeError` whose ``__cause__`` is the
    provider's :class:`~booley.runtime.agent_errors.ModelUnavailableError`.
    A timeout is an error too, so it never reads as acceptance.
    """
    try:
        with owned_tempdir("booley-model-probe-") as work:
            return _probe_model_in(project_root, booley_dir, model, work, timeout_s)
    except OSError as exc:  # fail-soft by contract: an unusable temp dir becomes a doctor SKIP
        raise ProbeError(f"model probe failed: {exc}") from exc


def _probe_model_in(
    project_root: Path, booley_dir: Path | None, model: str, work: Path, timeout_s: int
) -> ProbeUsage:
    """Run the model probe call in *work* (see :func:`probe_model_access`)."""
    import asyncio

    from booley.runtime import agent as agent_mod
    from booley.runtime import agent_config as config_mod
    from booley.runtime.agent_errors import UsageLimitError

    try:
        config_mod.load_backend_config(project_root, booley_dir=booley_dir)
        params = _probe_params(
            config_mod.get_backend_config().settings, model, work, timeout_s, "doctor-model-probe"
        )
    except Exception as exc:  # fail-soft by contract: environment issues become a doctor SKIP
        raise ProbeError(f"model probe failed: {exc}") from exc
    try:
        result = asyncio.run(agent_mod.call_agent(configure_agent_call(params)))
    except Exception as exc:
        raise ProbeError(
            f"model probe failed: {exc}",
            agent_failure=not isinstance(exc, UsageLimitError),
            agent_called=True,
        ) from exc
    usage = ProbeUsage.from_result(result)
    if result.timed_out:
        raise ProbeError(
            f"model probe timed out after {timeout_s}s", agent_called=True, usage=usage
        )
    return usage
