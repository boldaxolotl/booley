"""Doctor's developer memory probe (ADR 0028 Decision 12).

The container memory invariant needs a real number for "how much memory does
one ticket's Developer Agent cost?" — hardcoding it would drift with every agent
CLI/SDK release. ``booley doctor --deep`` therefore spawns the cheapest real
agent call (light-tier model, one turn, no MCP tools) and records the child
process's peak RSS into project runtime state at
``<project_dir>/runtime/developer_probe.json``. The invariant check reads
that file on every subsequent doctor run and multiplies it by the
``[jobs] max_tickets`` cap; until a measurement exists it uses a conservative
1 GiB fallback.

The probe is fail-soft by contract: no agent backend, no auth, or a timeout
must degrade doctor to the fallback (a SKIP), never crash it — every failure
mode surfaces as :class:`ProbeError` for the caller to report.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.core.boundary import as_positive_int
from booley.runtime.timefmt import utc_now_rfc3339

try:
    import resource  # POSIX-only; no Windows equivalent for RUSAGE_CHILDREN
except ImportError:  # pragma: no cover — Windows host
    resource = None  # type: ignore[assignment]

# Opaque token passed to getrusage(); resolved here so an injected getrusage
# works even where the resource module is absent.
_RUSAGE_CHILDREN = resource.RUSAGE_CHILDREN if resource is not None else -1

PROBE_FILENAME = "developer_probe.json"

# Developer Agent memory term used until `doctor --deep` has measured (ADR 0028
# Decision 12: "1g fallback until measured").
FALLBACK_BYTES = 1024**3

_PROBE_TIMEOUT_S = 300
# The cheapest real agent call: a single trivial turn with MCP tools disabled.
_PROBE_PROMPT = "Health probe: reply with the single word OK and stop."


class ProbeError(RuntimeError):
    """The probe agent could not run or produced no measurable child RSS.

    ``agent_failure`` distinguishes "the AGENT CALL itself failed" (auth
    failure, dead backend — a real project defect: every ticket's developer
    would fail the same way at launch) from environment limitations of the
    probe (no process accounting, no RSS reading). Doctor reports the former
    loud and degrades only the latter to a SKIP.
    """

    def __init__(self, message: str, *, agent_failure: bool = False) -> None:
        super().__init__(message)
        self.agent_failure = agent_failure


def probe_path(project_dir: Path) -> Path:
    """Location of the recorded measurement inside project runtime state.

    Lives beside the slot store (``runtime/jobs/slots``) — both are
    container-lifetime bookkeeping for the ADR 0028 admission story.
    """
    return project_dir / "runtime" / PROBE_FILENAME


def load_measurement(project_dir: Path) -> int | None:
    """Recorded developer peak RSS in bytes, or None when absent/invalid."""
    try:
        data = json.loads(probe_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("developer_peak_rss_bytes")
    # as_positive_int already rejects the bool trap and non-positive ints;
    # 0 is not a value it would ever return for genuinely-positive input, so
    # it doubles as the "invalid" sentinel here.
    return as_positive_int(value, 0) or None


def record_measurement(project_dir: Path, peak_rss_bytes: int) -> Path:
    """Atomically record a measurement; returns the file written."""
    path = probe_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "developer_peak_rss_bytes": int(peak_rss_bytes),
        "measured_at": utc_now_rfc3339(),
    }
    # tmp + rename so a concurrent reader never sees a partial file.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def measure_developer_rss(
    project_root: Path,
    *,
    timeout_s: int = _PROBE_TIMEOUT_S,
    getrusage: Callable[[int], Any] | None = None,
) -> tuple[int, bool]:
    """Spawn a short probe agent; return ``(peak_rss_bytes, exact)``.

    Both agent backends run their CLI/SDK as a child process, so
    ``getrusage(RUSAGE_CHILDREN).ru_maxrss`` (KiB on Linux) captures the
    high-water mark once the child is reaped. When the probe child sets a new
    high-water mark (``after > before``) the reading is exact; when an earlier
    doctor child was larger, ``after`` is still a safe *upper bound* for the
    probe — conservative in the invariant's favor — and ``exact`` is False.
    ``getrusage`` is injectable so tests never depend on real process
    accounting.

    Raises :class:`ProbeError` on any failure (backend unresolvable, no auth,
    agent error, timeout, or no child RSS observed) — the caller degrades to
    :data:`FALLBACK_BYTES`.
    """
    import asyncio

    from booley.config import settings as config_mod
    from booley.harness.models import AgentCallParams
    from booley.runtime import agent as agent_mod

    try:
        config_mod.load_models_config(project_root)
        if getrusage is None:
            if resource is None:
                raise ProbeError(
                    "child-process accounting (resource.getrusage) is "
                    "unavailable on this platform; using the fallback estimate"
                )
            getrusage = resource.getrusage
        cfg = config_mod.get_backend_config()
        params = AgentCallParams(
            prompt=_PROBE_PROMPT,
            model=cfg.model_for_tier("light"),
            cwd=str(project_root),
            allowed_agent_capabilities=[],
            max_turns=1,
            timeout_seconds=timeout_s,
            label="doctor-developer-probe",
        )
        before = getrusage(_RUSAGE_CHILDREN).ru_maxrss
    except Exception as exc:  # fail-soft by contract: environment issues become a doctor SKIP
        raise ProbeError(f"probe agent failed: {exc}") from exc
    try:
        result = asyncio.run(agent_mod.call_agent(params))
    except Exception as exc:
        # The agent call itself died — auth failure, dead backend. This is not
        # a probe limitation: every ticket's developer launch would fail the
        # same way (2026-07-23: expired seeded OAuth creds crashed every run
        # while doctor stayed green because this path degraded to SKIP).
        # A hit usage cap is the one agent-side error that is NOT a project
        # defect — it stays a SKIP.
        from booley.harness.blocking import UsageLimitError

        raise ProbeError(
            f"probe agent failed: {exc}",
            agent_failure=not isinstance(exc, UsageLimitError),
        ) from exc
    try:
        after = getrusage(_RUSAGE_CHILDREN).ru_maxrss
    except Exception as exc:  # fail-soft by contract
        raise ProbeError(f"probe agent failed: {exc}") from exc

    if result.timed_out:
        raise ProbeError(f"probe agent timed out after {timeout_s}s")
    if after <= 0:
        raise ProbeError("no child RSS observed around the probe agent call")
    return (int(after) * 1024, after > before)
