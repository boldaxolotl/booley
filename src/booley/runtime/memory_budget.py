"""The Sandbox's Memory Budget: what the Jobs may use of the container's memory.

The budget is the container's memory limit (``[sandbox].memory``, or installed
memory when the container has none) minus a reserve for what the Jobs do not own:
the running Developer Agents, the MCP server, ``t3 serve``, the headless Chrome
processes, and, without a swap allowance, a quarter of the base as headroom.
Memory admission holds each Job's Memory Reservation against this number; the
module computes and explains it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice, pairwise
from pathlib import Path

from booley.audit.resource_policy import (
    GIB_BYTES,
    SWAP_SHARE_DIVISOR,
    cgroup_memory_limit_bytes,
    cgroup_swap_limit_bytes,
    parse_meminfo,
)
from booley.runtime.pid import capture_process_identity

# A process found running whose memory cannot be read still reserves 1 GiB.
UNREADABLE_PROCESS_BYTES = GIB_BYTES
_MAX_PROCESSES = 8192
_CLAUDE_NAMES = frozenset({"claude", "claude.exe"})
_CODEX_NAMES = frozenset(
    {
        "codex",
        "codex-x86_64-unknown-linux-musl",
        "codex-aarch64-unknown-linux-musl",
        "codex-x86_64-unknown-linux-gnu",
        "codex-aarch64-unknown-linux-gnu",
    }
)
_MCP_SERVER_MODULE = "booley.mcp.server"
_MCP_SERVER_SCRIPT = "booley-mcp"
_T3_EXECUTABLE = "t3"
_T3_SERVE_COMMAND = "serve"
_HEADLESS_CHROME_NAME = "chrome-headless-shell"


class BudgetRule(StrEnum):
    """Which rule produced the budget."""

    LIMIT = "limit"  # container limit minus the reserve
    NO_LIMIT = "no-limit"  # installed memory minus the reserve; the container has no limit


class ReserveRole(StrEnum):
    """What a reserve part stands for."""

    DEVELOPER_AGENT = "developer-agent"
    MCP_SERVER = "mcp-server"
    T3_CODE = "t3-code"
    HEADLESS_CHROME = "headless-chrome"
    HEADROOM = "headroom"


class ReserveSource(StrEnum):
    """How a reserve part's size was obtained."""

    MEASURED = "measured"  # the process's current VmRSS
    UNREADABLE = "unreadable"  # found, but its memory could not be read: 1 GiB
    ABSENT = "absent"  # no such process is running: 0
    FIXED = "fixed"  # the no-swap headroom, a quarter of the base


@dataclass(frozen=True, slots=True)
class ReservePart:
    """One term of the reserve."""

    role: ReserveRole
    bytes: int
    source: ReserveSource
    pid: int | None  # None for the headroom, an absent role and the summed Chrome processes


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    """The budget with the inputs that explain it."""

    bytes: int
    rule: BudgetRule
    limit_bytes: int | None  # the container limit; None when the container has none
    installed_bytes: int | None  # read only when the container has no limit
    reserve: tuple[ReservePart, ...]
    swap_bytes: int = 0  # the swap the container may use; 0 without a limit or swap

    @property
    def reserve_bytes(self) -> int:
        """Total memory held back from the Jobs."""
        return sum(part.bytes for part in self.reserve)


def resolve_memory_budget(
    *,
    limit_bytes: int | None,
    installed_bytes: int | None,
    processes: Sequence[ReservePart],
    swap_bytes: int = 0,
) -> MemoryBudget:
    """Apply the budget rules to already-measured inputs (pure policy).

    A swap allowance counts only with a container limit; with one, swap absorbs
    a short overshoot, so no headroom is held back. Without one, a quarter of the
    base is held back instead: the room the default swap would give.
    """
    if limit_bytes is not None:
        rule, base = BudgetRule.LIMIT, limit_bytes
    elif installed_bytes is not None:
        rule, base = BudgetRule.NO_LIMIT, installed_bytes
    else:
        raise ValueError("memory budget needs a container limit or the installed memory")
    swap = swap_bytes if limit_bytes is not None else 0
    reserve = tuple(processes)
    if swap <= 0:
        reserve += (
            ReservePart(
                ReserveRole.HEADROOM, base // SWAP_SHARE_DIVISOR, ReserveSource.FIXED, None
            ),
        )
    # A reserve above the base clamps to 0; both stay in the result so it shows.
    budget = max(0, base - sum(part.bytes for part in reserve))
    return MemoryBudget(
        budget,
        rule,
        limit_bytes,
        installed_bytes if rule is BudgetRule.NO_LIMIT else None,
        reserve,
        max(0, swap),
    )


def _read_installed_bytes(proc_root: Path) -> int:
    """Return ``MemTotal`` from ``meminfo`` in bytes."""
    path = proc_root / "meminfo"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            if len(fields) == 3 and fields[1].isdigit() and fields[2].lower() == "kb":
                return int(fields[1]) * 1024
            break
    raise ValueError(f"{path} has no readable MemTotal line")


def _read_rss_bytes(proc_root: Path, pid: int) -> int | None:
    """Return the process's VmRSS in bytes, or ``None`` when unreadable."""
    try:
        text = (proc_root / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            fields = line.split()
            if len(fields) == 3 and fields[1].isdigit() and fields[2].lower() == "kb":
                return int(fields[1]) * 1024
    return None


def _read_executable_name(proc_root: Path, pid: int) -> str | None:
    try:
        target = str((proc_root / str(pid) / "exe").readlink())
    except OSError:
        return None
    return Path(target.removesuffix(" (deleted)")).name


def _read_comm(proc_root: Path, pid: int) -> str | None:
    try:
        return (proc_root / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_argv(proc_root: Path, pid: int) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [arg.decode("utf-8", "replace") for arg in raw.split(b"\0") if arg]


def _is_mcp_server(argv: Sequence[str]) -> bool:
    """Recognize ``python -m booley.mcp.server`` and the ``booley-mcp`` script."""
    for flag, module in pairwise(argv):
        if flag == "-m" and module == _MCP_SERVER_MODULE:
            return True
    return any(Path(arg).name == _MCP_SERVER_SCRIPT for arg in argv[:2])


def _is_t3_server(argv: Sequence[str]) -> bool:
    """Recognize ``t3 serve``, not another ``t3`` command or a script named ``t3``."""
    return len(argv) >= 2 and Path(argv[0]).name == _T3_EXECUTABLE and argv[1] == _T3_SERVE_COMMAND


def _classify(proc_root: Path, pid: int) -> ReserveRole | None:
    """Return the role of *pid* (agent, headless Chrome, MCP server, ``t3 serve``)."""
    executable = _read_executable_name(proc_root, pid)
    if (
        _read_comm(proc_root, pid) == "claude"
        or executable in _CLAUDE_NAMES
        or executable in _CODEX_NAMES
    ):
        return ReserveRole.DEVELOPER_AGENT
    if executable == _HEADLESS_CHROME_NAME:
        return ReserveRole.HEADLESS_CHROME
    argv = _read_argv(proc_root, pid)
    if _is_mcp_server(argv):
        return ReserveRole.MCP_SERVER
    return ReserveRole.T3_CODE if _is_t3_server(argv) else None


def _scan_processes(proc_root: Path) -> dict[ReserveRole, list[int]]:
    """Find the PIDs of each reserve role with one bounded scan of *proc_root*."""
    try:
        names = [entry.name for entry in islice(proc_root.iterdir(), _MAX_PROCESSES)]
    except OSError:
        names = []
    pids = sorted(int(name) for name in names if name.isdecimal() and int(name) > 0)
    found: dict[ReserveRole, list[int]] = {
        ReserveRole.DEVELOPER_AGENT: [],
        ReserveRole.MCP_SERVER: [],
        ReserveRole.T3_CODE: [],
        ReserveRole.HEADLESS_CHROME: [],
    }
    for pid in pids:
        role = _classify(proc_root, pid)
        if role is not None:
            found[role].append(pid)
    return found


def _measure_process(role: ReserveRole, pid: int, proc_root: Path) -> ReservePart:
    """Read one process's RSS; a vanished, reused or unreadable one counts 1 GiB."""
    before = capture_process_identity(pid, proc_root=proc_root)
    rss = _read_rss_bytes(proc_root, pid)
    after = capture_process_identity(pid, proc_root=proc_root)
    if before is None or before != after or rss is None:
        return ReservePart(role, UNREADABLE_PROCESS_BYTES, ReserveSource.UNREADABLE, pid)
    return ReservePart(role, rss, ReserveSource.MEASURED, pid)


def _measure_summed(role: ReserveRole, pids: Sequence[int], proc_root: Path) -> ReservePart:
    """Add the processes of *role* into one part, ``UNREADABLE`` if any member is."""
    members = [_measure_process(role, pid, proc_root) for pid in pids]
    source = (
        ReserveSource.UNREADABLE
        if any(m.source is ReserveSource.UNREADABLE for m in members)
        else ReserveSource.MEASURED
    )
    return ReservePart(role, sum(m.bytes for m in members), source, None)


def measure_reserve(*, proc_root: Path = Path("/proc")) -> tuple[ReservePart, ...]:
    """Measure the Developer Agents, MCP server, ``t3 serve`` and Chrome now running.

    Each process counts alone (its own VmRSS), not with its descendants: those
    include running Jobs, which their own reservations already cover. The
    headless Chrome processes count as one summed share. A role with no running
    process contributes an ``ABSENT`` part of 0 bytes.
    """
    parts: list[ReservePart] = []
    for role, pids in _scan_processes(proc_root).items():
        if not pids:
            parts.append(ReservePart(role, 0, ReserveSource.ABSENT, None))
        elif role is ReserveRole.HEADLESS_CHROME:
            parts.append(_measure_summed(role, pids, proc_root))
        else:
            parts.extend(_measure_process(role, pid, proc_root) for pid in pids)
    return tuple(parts)


def _swap_allowance_bytes(limit_bytes: int, proc_root: Path) -> int:
    """Return the swap the container may use: its cgroup allowance capped at the host's.

    0 when the cgroup states no swap limit (no swap accounting) or ``meminfo``
    is unreadable, which holds back a quarter of the base.
    """
    allowed = cgroup_swap_limit_bytes(limit_bytes)
    if allowed is None:
        return 0
    try:
        host = parse_meminfo((proc_root / "meminfo").read_text(encoding="utf-8")).swap_bytes
    except (OSError, ValueError):
        return 0
    return min(allowed, host)


def compute_memory_budget(*, proc_root: Path = Path("/proc")) -> MemoryBudget:
    """Compute the Memory Budget now.

    Raises ``OSError`` or ``ValueError`` for a missing or unreadable ``MemTotal``
    when the container has no limit.
    """
    limit = cgroup_memory_limit_bytes()
    installed = _read_installed_bytes(proc_root) if limit is None else None
    return resolve_memory_budget(
        limit_bytes=limit,
        installed_bytes=installed,
        processes=measure_reserve(proc_root=proc_root),
        swap_bytes=0 if limit is None else _swap_allowance_bytes(limit, proc_root),
    )
