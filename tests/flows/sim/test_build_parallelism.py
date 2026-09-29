"""Resource-policy tests for Verilator simulation-model compilation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from booley.audit.resource_policy import GIB_BYTES
from booley.flows.sim import build_parallelism
from booley.flows.sim.build_parallelism import (
    load_verilator_build_budget,
    verilator_backend_arguments,
)
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
    return lambda path: values.get(str(path))


def test_affinity_and_v2_quota_bound_each_heavy_lane() -> None:
    budget = load_verilator_build_budget(
        affinity_cpu_count=lambda: 6,
        read_text=_reader({"/sys/fs/cgroup/cpu.max": "350000 100000"}),
        config_loader=lambda: {"jobs": {"max_heavy": 2, "heavy_memory": "8g"}},
    )

    assert budget.effective_cpu_count == 3
    assert budget.heavy_lane_count == 2
    assert budget.make_jobs == 1


def test_affinity_below_host_cpu_count_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_parallelism.os, "sched_getaffinity", lambda _pid: {0, 1, 2})
    monkeypatch.setattr(build_parallelism.os, "cpu_count", lambda: 32)

    assert build_parallelism._affinity_cpu_count() == 3


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
def test_cgroup_cpu_variants(values: dict[str, str], expected: int) -> None:
    budget = load_verilator_build_budget(
        affinity_cpu_count=lambda: 8,
        read_text=_reader(values),
        config_loader=lambda: {"jobs": {"heavy_memory": "16g"}},
    )

    assert budget.effective_cpu_count == expected
    assert budget.make_jobs == expected


@pytest.mark.parametrize(
    ("config", "memory_limit", "expected_memory", "expected_jobs"),
    (
        ({"jobs": {"heavy_memory": "2g"}}, None, 2 * GIB_BYTES, 2),
        ({"jobs": {}}, None, 4 * GIB_BYTES, 4),
        ({"jobs": {"heavy_memory": "invalid"}}, None, 4 * GIB_BYTES, 4),
        (
            {"jobs": {"max_heavy": 2, "heavy_memory": "8g"}},
            str(6 * GIB_BYTES),
            3 * GIB_BYTES,
            3,
        ),
    ),
    ids=("configured", "default", "invalid-fallback", "finite-cgroup-hard-cap"),
)
def test_heavy_memory_reservation_bounds_compiler_jobs(
    config: dict,
    memory_limit: str | None,
    expected_memory: int,
    expected_jobs: int,
) -> None:
    values = {"/sys/fs/cgroup/memory.max": memory_limit} if memory_limit else {}
    budget = load_verilator_build_budget(
        affinity_cpu_count=lambda: 16,
        read_text=_reader(values),
        config_loader=lambda: config,
    )

    assert budget.memory_bytes == expected_memory
    assert budget.make_jobs == expected_jobs


def test_unreserved_lane_stays_serial_and_divided_cpu_clamps_to_one() -> None:
    def config() -> dict:
        return {"jobs": {"max_heavy": 16, "heavy_memory": "16g"}}

    heavy = load_verilator_build_budget(
        affinity_cpu_count=lambda: 4, read_text=lambda _path: None, config_loader=config
    )
    unreserved = load_verilator_build_budget(
        "unreserved",
        affinity_cpu_count=lambda: 64,
        read_text=lambda _path: None,
        config_loader=config,
    )

    assert heavy.make_jobs == 1
    assert unreserved.make_jobs == 1


@pytest.mark.parametrize(
    "option",
    ("-j", "-j8", "--jobs", "--jobs=8", "-kj4", "-j 8", "--jobs 8"),
)
@pytest.mark.parametrize("api", ("modern", "legacy"))
def test_authored_jobs_options_are_preserved_without_injection(option: str, api: str) -> None:
    kwargs = {api: (option,)}
    assert verilator_backend_arguments(_inspection(**kwargs)) == ()


@pytest.mark.parametrize("option", ("-junk", "--job=8", "OPT_FAST=-O3"))
def test_jobs_option_lookalikes_do_not_disable_parallel_policy(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.build_parallelism.load_verilator_build_budget",
        lambda _lane="heavy": SimpleNamespace(make_jobs=3),
    )

    assert verilator_backend_arguments(_inspection(modern=(option,))) == (
        "-j3",
        "VM_PARALLEL_BUILDS=1",
    )


def test_non_verilator_receives_no_backend_arguments() -> None:
    assert verilator_backend_arguments(_inspection(eda_tool="icarus")) == ()


def test_identical_inputs_produce_identical_ordered_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.build_parallelism.load_verilator_build_budget",
        lambda _lane="heavy": SimpleNamespace(make_jobs=5),
    )
    inspection = _inspection(modern=("OPT_FAST=-O3",))

    first = verilator_backend_arguments(inspection)
    second = verilator_backend_arguments(inspection)

    assert first == second == ("-j5", "VM_PARALLEL_BUILDS=1")
