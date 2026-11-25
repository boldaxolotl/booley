"""Resource-policy tests for Verilator simulation-model compilation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.memory_admission import JOB_THREADS_ENV
from booley.flows.sim import build_parallelism
from booley.flows.sim.build_parallelism import verilator_backend_arguments
from booley.runtime.cpu_budget import available_cpu_count
from booley.targets.domain import TargetHandle, TargetInspection


def _inspection(
    *, eda_tool: str = "verilator", modern: tuple[str, ...] = (), legacy: tuple[str, ...] = ()
) -> TargetInspection:
    handle = cast(TargetHandle, SimpleNamespace())
    flow_options = {"make_options": modern} if modern else {}
    tool_options = {"make_options": legacy} if legacy else {}
    return TargetInspection(
        handle=handle,
        toplevel="tb",
        flow="sim",
        eda_tool=eda_tool,
        flow_options=flow_options,
        parameters={},
        inputs=(),
        tool_options=tool_options,
    )


@pytest.mark.parametrize("error", [FileNotFoundError("absent"), PermissionError("denied")])
def test_cgroup_reader_ignores_absent_files_but_surfaces_access_failures(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise error

    monkeypatch.setattr(Path, "read_text", read_text)
    if isinstance(error, FileNotFoundError):
        assert available_cpu_count(lambda: 8) == 8
    else:
        with pytest.raises(PermissionError, match="denied"):
            available_cpu_count(lambda: 8)


def _cores(monkeypatch: pytest.MonkeyPatch, affinity: int, files: dict[str, str]) -> None:
    """The container's cores: the affinity set, bounded by the cgroup files *files*.

    Private seam: verilator_backend_arguments passes these build_parallelism names to
    available_cpu_count, so only they reach the build's -j (endpoint admission's
    eight_cores fixture patches the same names).
    """
    monkeypatch.setattr(build_parallelism, "_affinity_cpu_count", lambda: affinity)
    monkeypatch.setattr(build_parallelism, "_read_text", lambda path: files.get(path.as_posix()))


def _make_jobs(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    monkeypatch.delenv(JOB_THREADS_ENV, raising=False)
    return verilator_backend_arguments(_inspection())


def _probe_fails(_pid: int) -> set[int]:
    raise OSError("affinity unavailable")


@pytest.mark.parametrize(
    ("affinity", "host_cpus", "expected"),
    ((lambda _pid: {0, 1, 2}, 32, 3), (None, 7, 7), (_probe_fails, None, 1)),
    ids=("affinity-below-host", "no-affinity-api", "probe-fails"),
)
def test_affinity_bounds_the_cpu_count(
    monkeypatch: pytest.MonkeyPatch, affinity, host_cpus: int | None, expected: int
) -> None:
    if affinity is None:
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    else:
        monkeypatch.setattr(os, "sched_getaffinity", affinity, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: host_cpus)

    assert available_cpu_count(read_text=lambda _path: None) == expected


@pytest.mark.parametrize(
    ("affinity", "values", "expected"),
    (
        (6, {"/sys/fs/cgroup/cpu.max": "350000 100000"}, 3),
        (8, {"/sys/fs/cgroup/cpu.max": "max 100000"}, 8),
        (
            8,
            {
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "250000",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
            },
            2,
        ),
        (
            8,
            {
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "-1",
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000",
            },
            8,
        ),
    ),
    ids=("v2-quota", "v2-unlimited", "v1-fractional", "v1-unlimited"),
)
def test_cgroup_cpu_variants(
    monkeypatch: pytest.MonkeyPatch, affinity: int, values: dict[str, str], expected: int
) -> None:
    _cores(monkeypatch, affinity, values)

    assert _make_jobs(monkeypatch) == (f"-j{expected}", "VM_PARALLEL_BUILDS=1")


def test_unreserved_lane_stays_serial_and_does_not_probe_shared_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object) -> int:
        pytest.fail("unreserved lane probed shared resources")

    monkeypatch.setattr(build_parallelism, "available_cpu_count", unexpected)
    monkeypatch.setenv(JOB_THREADS_ENV, "8")

    assert verilator_backend_arguments(_inspection(), lane_kind="unreserved") == (
        "-j1",
        "VM_PARALLEL_BUILDS=1",
    )


@pytest.mark.parametrize("option", ("-j", "-j8", "--jobs", "--jobs=8", "-kj4", "-j 8", "--jobs 8"))
@pytest.mark.parametrize("api", ("modern", "legacy"))
def test_authored_jobs_options_are_preserved_without_injection(option: str, api: str) -> None:
    kwargs = {api: (option,)}
    assert verilator_backend_arguments(_inspection(**kwargs)) == ()


@pytest.mark.parametrize(
    "option", ("-junk", "--job=8", "OPT_FAST=-O3", "-I/x/proj", "-fobj", "-Wno-j", "-I/usr/j2")
)
def test_jobs_option_lookalikes_do_not_disable_parallel_policy(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    monkeypatch.setenv(JOB_THREADS_ENV, "3")

    assert verilator_backend_arguments(_inspection(modern=(option,))) == (
        "-j3",
        "VM_PARALLEL_BUILDS=1",
    )


def test_non_verilator_receives_no_backend_arguments() -> None:
    assert verilator_backend_arguments(_inspection(eda_tool="icarus")) == ()


def test_admitted_lane_uses_the_job_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Job's share of the cores, fixed at its admission; without one, every core."""
    _cores(monkeypatch, 8, {})
    inspection = _inspection()

    # No Job count (nothing else admitted): every available core.
    monkeypatch.delenv(JOB_THREADS_ENV, raising=False)
    assert verilator_backend_arguments(inspection) == ("-j8", "VM_PARALLEL_BUILDS=1")
    # The Job's count wins over the cores.
    monkeypatch.setenv(JOB_THREADS_ENV, "2")
    assert verilator_backend_arguments(inspection) == ("-j2", "VM_PARALLEL_BUILDS=1")
