"""Measure one Job command's resources and add them to the resource history.

Every local Flow command is sampled while it runs: process-tree RSS, the size
of the command's own temporary directory (``TMPDIR``, which lives on the
Sandbox's tmpfs and is charged to the container's memory limit), CPU time,
wall-clock time and the container's cgroup memory. Measurement only observes:
it never changes a Job's verdict, scheduling or exit code, and a value that
could not be measured is recorded as absent (``None``), never as zero.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.runtime import job_slots
from booley.runtime.pid import is_pid_alive
from booley.runtime.resource_history import append_records
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")
_CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
# Bounds one sample's walk of a temporary directory.
_SAMPLE_ENTRY_LIMIT = 100_000

# Synthesis runs sv2v, Yosys and OpenROAD under one ``make``; attribute each
# tool's processes (by ``/proc/<pid>/comm``) to its own history phase.
SYNTH_PHASES: Mapping[str, str] = {
    "sv2v": "sv2v",
    "yosys": "yosys",
    "yosys-abc": "yosys",
    "openroad": "openroad",
}


@dataclass(frozen=True)
class ResourceLabel:
    """History key for the commands that follow: Target, phase and Test Run."""

    target: str
    phase: str
    tests: tuple[str, ...] = ()
    # Process name -> phase, for one command that runs several phases.
    split: Mapping[str, str] | None = None


_label: ContextVar[ResourceLabel | None] = ContextVar("booley_resource_label", default=None)


def label_commands(target: str, phase: str, *, tests: tuple[str, ...] = ()) -> None:
    """Label the following commands in this context (one lifecycle stage)."""
    _label.set(ResourceLabel(target, phase, tuple(tests)))


def clear_label() -> None:
    """Forget a stage label so it cannot leak into a later invocation."""
    _label.set(None)


@contextlib.contextmanager
def resource_phase(
    target: str, phase: str, *, split: Mapping[str, str] | None = None
) -> Iterator[None]:
    """Label the commands run inside the block."""
    token = _label.set(ResourceLabel(target, phase, split=split))
    try:
        yield
    finally:
        _label.reset(token)


def current_label() -> ResourceLabel | None:
    """Return the label set by the enclosing lifecycle stage, if any."""
    return _label.get()


def cgroup_oom_kill_count() -> int | None:
    """Return this cgroup's cumulative OOM-kill count when available."""
    try:
        text = _CGROUP_MEMORY_EVENTS.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, raw = line.partition(" ")
        if key == "oom_kill":
            return int(raw) if raw.strip().isdigit() else None
    return None


def _container_memory_bytes() -> int | None:
    try:
        return int(_CGROUP_MEMORY_CURRENT.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _children_cpu_s() -> tuple[float, float] | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (ImportError, OSError):
        return None
    return usage.ru_utime, usage.ru_stime


@dataclass(frozen=True)
class _Proc:
    comm: str
    rss_bytes: int
    user_ticks: int
    sys_ticks: int


def _read_proc(pid: int) -> _Proc | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    head, _, rest = text.rpartition(")")
    fields = rest.split()
    # ``fields[0]`` is stat field 3: utime=14, stime=15, rss (pages)=24.
    try:
        return _Proc(
            head.partition("(")[2],
            int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            int(fields[11]),
            int(fields[12]),
        )
    except (IndexError, ValueError):
        return None


def process_tree(root_pid: int) -> dict[int, _Proc] | None:
    """Sample *root_pid* and its descendants; ``None`` when ``/proc`` is absent.

    The direct child is often ``make`` or ``sh`` while the memory lives in
    tool grandchildren, so the whole tree is summed. Linux exposes the
    descendants through ``task/<pid>/children`` without psutil.
    """
    if not Path("/proc").is_dir():
        return None
    pending = [root_pid]
    found: dict[int, _Proc] = {}
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        with contextlib.suppress(OSError, ValueError):
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
            pending.extend(int(token) for token in children.split())
        proc = _read_proc(pid)
        if proc is not None:
            found[pid] = proc
    return found


def directory_bytes(root: Path) -> int:
    """Allocated size of the files under *root* (tmpfs pages, not apparent size)."""
    total = 0
    pending = [root]
    entries = 0
    while pending and entries < _SAMPLE_ENTRY_LIMIT:
        try:
            with os.scandir(pending.pop()) as listing:
                for entry in listing:
                    entries += 1
                    with contextlib.suppress(OSError):
                        info = entry.stat(follow_symlinks=False)
                        blocks = getattr(info, "st_blocks", None)
                        total += info.st_size if blocks is None else blocks * 512
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
        except OSError:
            continue
    return total


def _peak(current: int | None, value: int | None) -> int | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


@dataclass
class Usage:
    """Peaks observed for one command or for one phase inside it."""

    peak_rss: int | None = None
    peak_tmp: int | None = None
    peak_memory: int | None = None
    container_peak: int | None = None  # growth of the container's cgroup memory
    first_seen: float | None = None
    last_seen: float | None = None
    ticks: dict[int, tuple[int, int]] = field(default_factory=dict)

    def add(self, now: float, procs: dict[int, _Proc] | None, tmp: int, growth: int | None):
        """Fold one sample in; *procs* is ``None`` when RSS cannot be read."""
        self.peak_tmp = _peak(self.peak_tmp, tmp)
        self.container_peak = _peak(self.container_peak, growth)
        if procs is None:
            return
        rss = sum(proc.rss_bytes for proc in procs.values())
        # An empty tree (the command already exited) still counts its TMPDIR files.
        self.peak_memory = _peak(self.peak_memory, rss + tmp)
        if procs:
            self.peak_rss = _peak(self.peak_rss, rss)
            self.first_seen = now if self.first_seen is None else self.first_seen
            self.last_seen = now
        for pid, proc in procs.items():
            user, system = self.ticks.get(pid, (0, 0))
            self.ticks[pid] = (max(user, proc.user_ticks), max(system, proc.sys_ticks))

    def seen_s(self) -> float | None:
        """Time between the first and last sample that saw a process."""
        if self.first_seen is None or self.last_seen is None:
            return None
        return self.last_seen - self.first_seen

    def sampled_cpu_s(self) -> tuple[float, float] | None:
        """CPU seen in samples; misses each process's last interval."""
        if not self.ticks:
            return None
        hertz = os.sysconf("SC_CLK_TCK")
        return (
            sum(user for user, _ in self.ticks.values()) / hertz,
            sum(system for _, system in self.ticks.values()) / hertz,
        )


class _InProcessCommands:
    """Tell whether a command ran with no other command in this process.

    ``RUSAGE_CHILDREN`` is per process, so its delta is this command's CPU
    only when no other command of the same Job ran meanwhile.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._starts = 0

    def enter(self) -> int | None:
        with self._lock:
            self._starts += 1
            self._active += 1
            return self._starts if self._active == 1 else None

    def leave(self, ticket: int | None) -> bool:
        with self._lock:
            self._active -= 1
            return ticket is not None and ticket == self._starts


_IN_PROCESS = _InProcessCommands()


def _other_jobs_hold_slots() -> bool | None:
    """Whether another live Job holds a Job Slot; ``None`` when unknown.

    This process's own slots, and the parent's slot that a baseline child
    inherits, are not other Jobs; a dead holder's stale entry is ignored.
    """
    root = job_slots.slots_dir()
    if root is None:
        return None
    store = job_slots.SlotStore(root)
    try:
        holders = [token for name in job_slots.JOB_CLASSES for token in store.observe(name)[0]]
    except OSError:
        return None
    own = {os.getpid(), os.getppid()}
    return any(token.pid not in own and is_pid_alive(token.pid) for token in holders)


@dataclass(frozen=True)
class JobMeasurement:
    """Everything measured over one command."""

    interval_s: float
    overall: Usage
    phases: dict[str, Usage]
    cpu_s: tuple[float, float] | None
    oom_kill_delta: int | None
    solo: bool | None

    @property
    def peak_rss_mb(self) -> float | None:
        peak = self.overall.peak_rss
        return peak / _MIB if peak is not None else None


class JobResourceMonitor:
    """Sample one local command, with its private ``TMPDIR``, from start to reap."""

    def __init__(self, split: Mapping[str, str] | None = None, *, interval_s: float = 0.25):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="booley-job-"))
        self._split = split or {}
        self._interval_s = interval_s
        self._overall = Usage()
        self._phases: dict[str, Usage] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._measurement: JobMeasurement | None = None
        self._pid: int | None = None

    def temp_env(self) -> dict[str, str]:
        """Point the command's temporary files into its private directory."""
        return dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(self.tmp_dir))

    def start(self, pid: int) -> None:
        self._pid = pid
        self._ticket = _IN_PROCESS.enter()
        self._oom_before = cgroup_oom_kill_count()
        self._cpu_before = _children_cpu_s()
        self._container_start = _container_memory_bytes()
        self._others_before = _other_jobs_hold_slots()
        self._sample()
        self._thread.start()

    def finish(self) -> JobMeasurement:
        """Stop sampling once the command is reaped; later calls return the same result."""
        if self._pid is None:  # the command never started
            return JobMeasurement(self._interval_s, Usage(), {}, None, None, None)
        if self._measurement is None:
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join(timeout=2)
            self._sample()
            self._measurement = self._measure()
        return self._measurement

    def _measure(self) -> JobMeasurement:
        alone = _IN_PROCESS.leave(self._ticket)
        others = (self._others_before, _other_jobs_hold_slots())
        oom_after, cpu_after = cgroup_oom_kill_count(), _children_cpu_s()
        # Exact children CPU when alone; otherwise the per-process samples.
        cpu = self._overall.sampled_cpu_s()
        if alone and self._cpu_before is not None and cpu_after is not None:
            cpu = (cpu_after[0] - self._cpu_before[0], cpu_after[1] - self._cpu_before[1])
        oom = None
        if self._oom_before is not None and oom_after is not None:
            oom = max(0, oom_after - self._oom_before)
        return JobMeasurement(
            interval_s=self._interval_s,
            overall=self._overall,
            phases=dict(self._phases),
            cpu_s=cpu,
            oom_kill_delta=oom,
            solo=None if None in others else alone and not any(others),
        )

    def _sample(self) -> None:
        now = time.monotonic()
        procs = process_tree(self._pid)
        tmp = directory_bytes(self.tmp_dir)
        container = _container_memory_bytes()
        growth = None
        if container is not None and self._container_start is not None:
            growth = max(0, container - self._container_start)
        with self._lock:
            self._overall.add(now, procs, tmp, growth)
            for phase in set(self._split.values()):
                members = {
                    pid: proc
                    for pid, proc in (procs or {}).items()
                    if self._split.get(proc.comm) == phase
                }
                if members:
                    self._phases.setdefault(phase, Usage()).add(now, members, tmp, growth)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._sample()


def _mb(value: int | None) -> float | None:
    return round(value / _MIB, 1) if value is not None else None


def _seconds(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def history_records(
    flow: str,
    label: ResourceLabel,
    flags: tuple[str, ...],
    measurement: JobMeasurement,
    result: Any,
) -> list[dict[str, Any]]:
    """One record for the command, plus one per phase it was split into."""
    outcome = "exit_zero" if result.returncode == 0 else "exit_nonzero"
    # The OOM counter is container-wide: blame this command only when it failed.
    if measurement.oom_kill_delta and result.returncode != 0:
        outcome = "oom_kill"
    common = {
        "flow": flow,
        "finished_at": utc_now_rfc3339(),
        "outcome": "timeout" if result.timed_out else outcome,
        "returncode": result.returncode,
        "oom_kill_delta": measurement.oom_kill_delta,
        "solo": measurement.solo,
        "sample_interval_s": measurement.interval_s,
    }
    rows = [(label.phase, measurement.overall, result.duration_s, measurement.cpu_s)]
    rows += [
        (name, u, u.seen_s(), u.sampled_cpu_s()) for name, u in sorted(measurement.phases.items())
    ]
    tests = ",".join(label.tests)
    return [
        {
            **common,
            "key": {
                "target": label.target,
                "phase": phase,
                "flags": sorted(flags),
                "test_run": tests,
            },
            # RSS never seen (the command exited before a sample): memory unknown.
            "peak_memory_mb": _mb(usage.peak_memory) if usage.peak_rss is not None else None,
            "peak_rss_mb": _mb(usage.peak_rss),
            "peak_tmp_mb": _mb(usage.peak_tmp),
            "cpu_user_s": _seconds(cpu[0]) if cpu else None,
            "cpu_sys_s": _seconds(cpu[1]) if cpu else None,
            "wall_s": _seconds(wall),
            "container_peak_delta_mb": _mb(usage.container_peak) if measurement.solo else None,
        }
        for phase, usage, wall, cpu in rows
    ]


def record_command(
    flow: str,
    label: ResourceLabel,
    flags: tuple[str, ...],
    measurement: JobMeasurement,
    result: Any,
) -> None:
    """Append the command's records to the Project history; never raise."""
    from booley.runtime.booley_dir import resolve_booley_dir

    try:
        booley_dir = resolve_booley_dir()
    except (OSError, RuntimeError, ValueError):
        return
    try:
        append_records(booley_dir, history_records(flow, label, flags, measurement, result))
    except (OSError, ValueError) as exc:
        logger.warning("Job resource history not recorded in %s: %s", booley_dir, exc)
