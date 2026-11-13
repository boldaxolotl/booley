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
from booley.targets.domain import TargetHandle, TargetInspection


def _inspection(
    *,
    eda_tool: str = "verilator",
    modern: tuple[str, ...] = (),
    legacy: tuple[str, ...] = (),
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


def _reader(values: dict[str, str]):
    return lambda path: values.get(path.as_posix())


def test_cgroup_reader_ignores_absent_files_but_surfaces_access_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    assert build_parallelism._read_text(missing) is None

    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied"))
    )
    with pytest.raises(PermissionError, match="denied"):
        build_parallelism._read_text(tmp_path / "denied")


def _cores(monkeypatch: pytest.MonkeyPatch, affinity: int, files: dict[str, str]) -> None:
    """The container's cores: the affinity set, bounded by the cgroup files *files*."""
    monkeypatch.setattr(build_parallelism, "_affinity_cpu_count", lambda: affinity)
    monkeypatch.setattr(build_parallelism, "_read_text", _reader(files))


def _make_jobs(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    monkeypatch.delenv(JOB_THREADS_ENV, raising=False)
    return verilator_backend_arguments(_inspection())


def test_affinity_and_v2_quota_bound_the_admitted_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    _cores(monkeypatch, 6, {"/sys/fs/cgroup/cpu.max": "350000 100000"})

    assert _make_jobs(monkeypatch) == ("-j3", "VM_PARALLEL_BUILDS=1")


def test_affinity_below_host_cpu_count_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        os,
        "sched_getaffinity",
        lambda _pid: {0, 1, 2},
        raising=False,
    )
    monkeypatch.setattr(os, "cpu_count", lambda: 32)

    assert build_parallelism._affinity_cpu_count() == 3


def test_affinity_falls_back_when_platform_has_no_affinity_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 7)

    assert build_parallelism._affinity_cpu_count() == 7


def test_affinity_falls_back_when_platform_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_pid: int) -> set[int]:
        raise OSError("affinity unavailable")

    monkeypatch.setattr(os, "sched_getaffinity", unavailable, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert build_parallelism._affinity_cpu_count() == 1


@pytest.mark.parametrize(
    ("values", "expected"),
    (
        ({"/sys/fs/cgroup/cpu.max": "max 100000"}, 8),
        (
            {
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "250000",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
            },
            2,
        ),
        (
            {
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "-1",
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000",
            },
            8,
        ),
    ),
    ids=("v2-unlimited", "v1-fractional", "v1-unlimited"),
)
def test_cgroup_cpu_variants(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], expected: int
) -> None:
    _cores(monkeypatch, 8, values)

    assert _make_jobs(monkeypatch) == (f"-j{expected}", "VM_PARALLEL_BUILDS=1")


def test_unreserved_lane_stays_serial_and_does_not_probe_shared_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object) -> int:
        pytest.fail("unreserved lane probed shared resources")

    monkeypatch.setattr(build_parallelism, "_affinity_cpu_count", unexpected)
    monkeypatch.setattr(build_parallelism, "_read_text", unexpected)
    monkeypatch.setenv(JOB_THREADS_ENV, "8")

    assert verilator_backend_arguments(_inspection(), lane_kind="unreserved") == (
        "-j1",
        "VM_PARALLEL_BUILDS=1",
    )


@pytest.mark.parametrize(
    "option",
    ("-j", "-j8", "--jobs", "--jobs=8", "-kj4", "-j 8", "--jobs 8"),
)
@pytest.mark.parametrize("api", ("modern", "legacy"))
def test_authored_jobs_options_are_preserved_without_injection(option: str, api: str) -> None:
    kwargs = {api: (option,)}
    assert verilator_backend_arguments(_inspection(**kwargs)) == ()


@pytest.mark.parametrize(
    "option",
    ("-junk", "--job=8", "OPT_FAST=-O3", "-I/x/proj", "-fobj", "-Wno-j", "-I/usr/j2"),
)
def test_jobs_option_lookalikes_do_not_disable_parallel_policy(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
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
    # Without a Job count the cores are bounded by the container's CPU quota.
    monkeypatch.delenv(JOB_THREADS_ENV)
    _cores(monkeypatch, 8, {"/sys/fs/cgroup/cpu.max": "400000 100000"})
    assert verilator_backend_arguments(inspection) == ("-j4", "VM_PARALLEL_BUILDS=1")
