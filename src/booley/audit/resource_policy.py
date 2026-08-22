"""Memory admission and synthesis-calibration policy for environment audits."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import as_dict, as_str

GIB_BYTES = 1024**3
DEFAULT_HEAVY_JOB_BYTES = 4 * GIB_BYTES
MEMORY_HEADROOM_BYTES = 2 * GIB_BYTES
SYNTHESIS_MARGIN_PERCENT = 15

CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_CGROUP_UNLIMITED_FLOOR = 1 << 60
_MEMORY_SUFFIXES = {"b": 1, "k": 1024, "m": 1024**2, "g": GIB_BYTES}


@dataclass(frozen=True, slots=True)
class SynthesisMemoryCalibration:
    """Peak memory observed for one completed synthesis Target."""

    target: str
    peak_rss_bytes: int


@dataclass(frozen=True, slots=True)
class HeavyMemoryReservation:
    """The effective HEAVY-job reservation and the evidence behind it."""

    bytes: int
    evidence: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRequirement:
    """Resource terms that determine the minimum safe container memory."""

    max_heavy: int
    heavy_job_bytes: int
    max_tickets: int
    developer_bytes: int
    headroom_bytes: int = MEMORY_HEADROOM_BYTES

    @property
    def required_bytes(self) -> int:
        """Total memory required when every admitted workload coincides."""
        return (
            self.max_heavy * self.heavy_job_bytes
            + self.max_tickets * self.developer_bytes
            + self.headroom_bytes
        )


def format_memory(byte_count: int | float) -> str:
    """Format a byte count in GiB the way Booley configuration spells it."""
    gib = byte_count / GIB_BYTES
    if gib == int(gib):
        return f"{int(gib)}g"
    return f"{gib:.1f}g"


def parse_memory_limit(text: str) -> int | None:
    """Parse a Docker-style memory string such as ``6g`` or ``512m``."""
    match = re.fullmatch(r"(\d+)\s*([bkmg]?)", text.strip().lower())
    if not match:
        return None
    return int(match.group(1)) * _MEMORY_SUFFIXES[match.group(2) or "b"]


def cgroup_memory_limit_bytes() -> int | None:
    """Return the effective cgroup limit, or ``None`` when it is unlimited."""
    for path in CGROUP_MEMORY_LIMIT_PATHS:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text == "max":
            return None
        try:
            value = int(text)
        except ValueError:
            continue
        return None if value >= _CGROUP_UNLIMITED_FLOOR else value
    return None


def configured_sandbox_memory(config: Mapping[str, object]) -> str:
    """Return the configured sandbox limit, or an empty string when absent."""
    section = as_dict(config.get("sandbox"), default={}) or {}
    memory = section.get("memory", "")
    memory_table = as_dict(memory)
    if memory_table is not None:
        memory = memory_table.get("default", "")
    return (as_str(memory, "") or "").strip()


def heavy_memory_reservation(
    configured_memory: object,
    calibration: SynthesisMemoryCalibration | None,
    selected_targets: Sequence[str],
) -> HeavyMemoryReservation:
    """Resolve configured and measured evidence into one HEAVY reservation."""
    reservation = _configured_reservation(configured_memory)
    if reservation.error or calibration is None:
        return reservation
    calibration_error = _calibration_error(calibration, selected_targets)
    if calibration_error:
        return HeavyMemoryReservation(reservation.bytes, reservation.evidence, calibration_error)
    return _calibrated_reservation(reservation, calibration)


def _configured_reservation(configured_memory: object) -> HeavyMemoryReservation:
    if configured_memory is None:
        return HeavyMemoryReservation(DEFAULT_HEAVY_JOB_BYTES, "4g default")
    configured_text = as_str(configured_memory)
    if configured_text is None:
        return _unparseable_reservation(configured_memory)
    parsed = parse_memory_limit(configured_text)
    if parsed is None:
        return _unparseable_reservation(configured_memory)
    if parsed <= 0:
        return HeavyMemoryReservation(
            DEFAULT_HEAVY_JOB_BYTES,
            "4g default",
            "[jobs] heavy_memory must be greater than zero",
        )
    return HeavyMemoryReservation(parsed, f"configured {format_memory(parsed)}")


def _unparseable_reservation(configured_memory: object) -> HeavyMemoryReservation:
    return HeavyMemoryReservation(
        DEFAULT_HEAVY_JOB_BYTES,
        "4g default",
        f"[jobs] heavy_memory = {configured_memory!r} is unparseable; use a value such as '8g'",
    )


def _calibration_error(
    calibration: SynthesisMemoryCalibration,
    selected_targets: Sequence[str],
) -> str | None:
    if calibration.target not in selected_targets:
        return (
            f"stored synthesis memory calibration is for unselected Target "
            f"{calibration.target!r}; rerun doctor --deep over the current Doctor matrix"
        )
    return None


def _calibrated_reservation(
    configured: HeavyMemoryReservation,
    calibration: SynthesisMemoryCalibration,
) -> HeavyMemoryReservation:
    with_margin = calibration.peak_rss_bytes * (100 + SYNTHESIS_MARGIN_PERCENT) // 100
    measured = ((with_margin + GIB_BYTES - 1) // GIB_BYTES) * GIB_BYTES
    if measured <= configured.bytes:
        evidence = (
            f"{configured.evidence}; calibrated peak {format_memory(calibration.peak_rss_bytes)}"
        )
        return HeavyMemoryReservation(configured.bytes, evidence)
    evidence = (
        f"{format_memory(calibration.peak_rss_bytes)} measured on {calibration.target} "
        f"+ {SYNTHESIS_MARGIN_PERCENT}% margin"
    )
    return HeavyMemoryReservation(measured, evidence)


def memory_requirement(
    *,
    max_heavy: int,
    heavy_job_bytes: int,
    max_tickets: int,
    developer_bytes: int,
) -> MemoryRequirement:
    """Build the typed arithmetic behind Doctor's memory admission check."""
    return MemoryRequirement(max_heavy, heavy_job_bytes, max_tickets, developer_bytes)
