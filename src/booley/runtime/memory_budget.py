"""The Sandbox's Memory Budget: what the Jobs may use of the container's memory.

Default budget is the container's memory limit (or installed memory when the
container has none) minus a reserve for what the Jobs do not own: the running
Developer Agents, the MCP server, the Dashboard, and fixed headroom. Setting
``[jobs].memory_budget`` replaces the whole default. Nothing admits Jobs with
this yet; the module only computes and explains the number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice, pairwise
from pathlib import Path

from booley.audit.resource_policy import (
    GIB_BYTES,
    MEMORY_HEADROOM_BYTES,
    cgroup_memory_limit_bytes,
    configured_memory_budget,
)
from booley.runtime.chat_sessions import CODEX_NATIVE_NAMES
from booley.runtime.pid import capture_process_identity

# A process found running whose memory cannot be read still reserves 1 GiB.
UNREADABLE_PROCESS_BYTES = GIB_BYTES
# Prefix of the Dashboard's socket address as ``/proc/net/unix`` prints it (the
# abstract-namespace NUL shows as "@"); a test pins it to ``view_probe.socket_name``.
DASHBOARD_SOCKET_PREFIX = "@booley-dashboard:"

_MAX_PROCESSES = 8192
_MAX_FDS_PER_PROCESS = 4096
_CLAUDE_NAMES = frozenset({"claude", "claude.exe"})
_MCP_SERVER_MODULE = "booley.mcp.server"
_MCP_SERVER_SCRIPT = "booley-mcp"


class BudgetRule(StrEnum):
    """Which rule produced the budget."""

    LIMIT = "limit"  # container limit minus the reserve
    NO_LIMIT = "no-limit"  # installed memory minus the reserve; the container has no limit
    OVERRIDE = "override"  # ``[jobs].memory_budget``


class ReserveRole(StrEnum):
    """What a reserve part stands for."""

    DEVELOPER_AGENT = "developer-agent"
    MCP_SERVER = "mcp-server"
    DASHBOARD = "dashboard"
    HEADROOM = "headroom"


class ReserveSource(StrEnum):
    """How a reserve part's size was obtained."""

    MEASURED = "measured"  # the process's current VmRSS
    UNREADABLE = "unreadable"  # found, but its memory could not be read: 1 GiB
    ABSENT = "absent"  # no such process is running: 0
    FIXED = "fixed"  # the fixed headroom


@dataclass(frozen=True, slots=True)
class ReservePart:
    """One term of the reserve."""

    role: ReserveRole
    bytes: int
    source: ReserveSource
    pid: int | None  # None for the headroom and for an absent role


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    """The budget with the inputs that explain it."""

    bytes: int
    rule: BudgetRule
    limit_bytes: int | None  # the container limit; None when the container has none
    installed_bytes: int | None  # read only when the container has no limit
    reserve: tuple[ReservePart, ...]  # empty for an override

    @property
    def reserve_bytes(self) -> int:
        """Total memory held back from the Jobs."""
        return sum(part.bytes for part in self.reserve)

    @property
    def exceeds_limit(self) -> bool:
        """Whether an override promises more than the container can give."""
        return (
            self.rule is BudgetRule.OVERRIDE
            and self.limit_bytes is not None
            and self.bytes > self.limit_bytes
        )


def resolve_memory_budget(
    *,
    override_bytes: int | None,
    limit_bytes: int | None,
    installed_bytes: int | None,
    processes: Sequence[ReservePart],
) -> MemoryBudget:
    """Apply the budget rules to already-measured inputs (pure policy)."""
    if override_bytes is not None:
        return MemoryBudget(override_bytes, BudgetRule.OVERRIDE, limit_bytes, None, ())
    if limit_bytes is not None:
        rule, base = BudgetRule.LIMIT, limit_bytes
    elif installed_bytes is not None:
        rule, base = BudgetRule.NO_LIMIT, installed_bytes
    else:
        raise ValueError("memory budget needs a container limit or the installed memory")
    headroom = ReservePart(ReserveRole.HEADROOM, MEMORY_HEADROOM_BYTES, ReserveSource.FIXED, None)
    reserve = (*processes, headroom)
    # A reserve above the base clamps to 0; both stay in the result so it shows.
    budget = max(0, base - sum(part.bytes for part in reserve))
    return MemoryBudget(
        budget,
        rule,
        limit_bytes,
        installed_bytes if rule is BudgetRule.NO_LIMIT else None,
        reserve,
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


def _classify(proc_root: Path, pid: int) -> ReserveRole | None:
    """Return the agent or MCP server role of *pid*; the Dashboard is found by socket."""
    executable = _read_executable_name(proc_root, pid)
    if (
        _read_comm(proc_root, pid) == "claude"
        or executable in _CLAUDE_NAMES
        or executable in CODEX_NATIVE_NAMES
    ):
        return ReserveRole.DEVELOPER_AGENT
    if _is_mcp_server(_read_argv(proc_root, pid)):
        return ReserveRole.MCP_SERVER
    return None


def _dashboard_socket_inode(proc_root: Path) -> str | None:
    """Return the inode of this PID namespace's Dashboard socket, if one is bound."""
    try:
        scope = str((proc_root / "self" / "ns" / "pid").readlink())
        table = (proc_root / "net" / "unix").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    address = DASHBOARD_SOCKET_PREFIX + scope
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[7] == address:
            return fields[6]
    return None


def _holds_socket(proc_root: Path, pid: int, inode: str) -> bool:
    wanted = f"socket:[{inode}]"
    try:
        links = (proc_root / str(pid) / "fd").iterdir()
        return any(str(link.readlink()) == wanted for link in islice(links, _MAX_FDS_PER_PROCESS))
    except OSError:
        return False


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
        ReserveRole.DASHBOARD: [],
    }
    inode = _dashboard_socket_inode(proc_root)
    for pid in pids:
        role = _classify(proc_root, pid)
        if role is not None:
            found[role].append(pid)
        elif (
            inode is not None
            and not found[ReserveRole.DASHBOARD]
            and _holds_socket(proc_root, pid, inode)
        ):
            found[ReserveRole.DASHBOARD].append(pid)
    return found


def _measure_process(role: ReserveRole, pid: int, proc_root: Path) -> ReservePart:
    """Read one process's RSS; a vanished, reused or unreadable one counts 1 GiB."""
    before = capture_process_identity(pid, proc_root=proc_root)
    rss = _read_rss_bytes(proc_root, pid)
    after = capture_process_identity(pid, proc_root=proc_root)
    if before is None or before != after or rss is None:
        return ReservePart(role, UNREADABLE_PROCESS_BYTES, ReserveSource.UNREADABLE, pid)
    return ReservePart(role, rss, ReserveSource.MEASURED, pid)


def measure_reserve(*, proc_root: Path = Path("/proc")) -> tuple[ReservePart, ...]:
    """Measure the Developer Agents, MCP server and Dashboard now running.

    Each process counts alone (its own VmRSS), not with its descendants: those
    include running Jobs, which their own reservations already cover. A role
    with no running process contributes an ``ABSENT`` part of 0 bytes.
    """
    parts: list[ReservePart] = []
    for role, pids in _scan_processes(proc_root).items():
        if not pids:
            parts.append(ReservePart(role, 0, ReserveSource.ABSENT, None))
        parts.extend(_measure_process(role, pid, proc_root) for pid in pids)
    return tuple(parts)


def compute_memory_budget(
    config: Mapping[str, object], *, proc_root: Path = Path("/proc")
) -> MemoryBudget:
    """Compute the Memory Budget for the project configuration *config*.

    Raises ``ValueError`` for an invalid ``[jobs].memory_budget`` and for an
    unreadable ``MemTotal`` when the container has no limit.
    """
    override = configured_memory_budget(config)
    limit = cgroup_memory_limit_bytes()
    if override is not None:
        return resolve_memory_budget(
            override_bytes=override, limit_bytes=limit, installed_bytes=None, processes=()
        )
    installed = _read_installed_bytes(proc_root) if limit is None else None
    return resolve_memory_budget(
        override_bytes=None,
        limit_bytes=limit,
        installed_bytes=installed,
        processes=measure_reserve(proc_root=proc_root),
    )
