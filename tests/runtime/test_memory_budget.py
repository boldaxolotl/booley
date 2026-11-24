"""The Sandbox Memory Budget: the computed rules and the measured reserve."""

from __future__ import annotations

import importlib
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import symlink_or_skip

MODULE = "booley.runtime.memory_budget"


class _LazyModule:
    """Import the module under test on first use, so a missing module fails a test body."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module(MODULE), name)


memory_budget = _LazyModule()
GIB = 1024**3
MIB = 1024**2
SCOPE = "pid:[17]"
SOCKET_INODE = "5555"


def part(role: Any, size: int, pid: int | None = 4_200_000) -> Any:
    return memory_budget.ReservePart(role, size, memory_budget.ReserveSource.MEASURED, pid)


def add_process(
    proc: Path,
    pid: int,
    *,
    comm: str | None = None,
    exe: str | None = None,
    argv: tuple[str, ...] = (),
    rss_kib: int | None = None,
    start: int = 7,
    ppid: int = 1,
    sockets: tuple[str, ...] = (),
) -> None:
    """Build one fake ``/proc/<pid>`` entry; absent arguments leave the file out."""
    directory = proc / str(pid)
    (directory / "ns").mkdir(parents=True)
    symlink_or_skip(directory / "ns/pid", Path(SCOPE))
    fields = ["S", str(ppid), *["0"] * 17, str(start), "0"]
    (directory / "stat").write_text(f"{pid} (x y) " + " ".join(fields), encoding="utf-8")
    if comm is not None:
        (directory / "comm").write_text(comm + "\n", encoding="utf-8")
    if exe is not None:
        symlink_or_skip(directory / "exe", Path(exe))
    if argv:
        (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    if rss_kib is not None:
        (directory / "status").write_text(f"Name:\tx\nVmRSS:\t  {rss_kib} kB\n", encoding="utf-8")
    if sockets:
        (directory / "fd").mkdir()
        for number, inode in enumerate(sockets, start=3):
            symlink_or_skip(directory / "fd" / str(number), Path(f"socket:[{inode}]"))


def make_proc(tmp_path: Path, *, dashboard: bool = False) -> Path:
    """An empty fake ``/proc`` with this PID namespace and an optional Dashboard socket."""
    proc = tmp_path / "proc"
    (proc / "self/ns").mkdir(parents=True)
    symlink_or_skip(proc / "self/ns/pid", Path(SCOPE))
    (proc / "net").mkdir()
    rows = ["Num RefCount Protocol Flags Type St Inode Path"]
    rows.append("0000: 00000002 00000000 00010000 0001 01 9999 @unrelated")
    if dashboard:
        rows.append(
            f"0001: 00000002 00000000 00010000 0001 01 {SOCKET_INODE} @booley-dashboard:{SCOPE}"
        )
    (proc / "net/unix").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return proc


def test_limit_minus_measured_reserve_and_headroom() -> None:
    result = memory_budget.resolve_memory_budget(
        limit_bytes=32 * GIB,
        installed_bytes=None,
        processes=(
            part(memory_budget.ReserveRole.DEVELOPER_AGENT, GIB),
            part(memory_budget.ReserveRole.MCP_SERVER, 256 * MIB),
            part(memory_budget.ReserveRole.DASHBOARD, 256 * MIB),
        ),
    )

    assert result.bytes == 24159191040  # 32 GiB - (1.5 GiB measured + 8 GiB headroom)
    assert result.rule is memory_budget.BudgetRule.LIMIT
    assert result.limit_bytes == 32 * GIB
    assert result.installed_bytes is None
    assert result.reserve_bytes == 9 * GIB + GIB // 2
    assert [p.role for p in result.reserve] == [
        memory_budget.ReserveRole.DEVELOPER_AGENT,
        memory_budget.ReserveRole.MCP_SERVER,
        memory_budget.ReserveRole.DASHBOARD,
        memory_budget.ReserveRole.HEADROOM,
    ]
    assert result.reserve[-1].source is memory_budget.ReserveSource.FIXED
    assert result.reserve[-1].bytes == 8 * GIB  # a quarter of the 32 GiB limit


def test_reserve_above_the_base_clamps_to_zero_and_stays_visible() -> None:
    result = memory_budget.resolve_memory_budget(
        limit_bytes=GIB,
        installed_bytes=None,
        processes=(part(memory_budget.ReserveRole.DEVELOPER_AGENT, GIB),),
    )

    assert result.bytes == 0
    assert result.reserve_bytes == GIB + GIB // 4


def test_resolve_needs_a_limit_or_installed_memory() -> None:
    with pytest.raises(ValueError, match="container limit or the installed memory"):
        memory_budget.resolve_memory_budget(limit_bytes=None, installed_bytes=None, processes=())


def test_no_container_limit_uses_installed_memory_and_says_so() -> None:
    result = memory_budget.resolve_memory_budget(
        limit_bytes=None,
        installed_bytes=16 * GIB,
        processes=(part(memory_budget.ReserveRole.MCP_SERVER, GIB),),
    )

    assert result.bytes == 11 * GIB  # 16 GiB - 1 GiB measured - 4 GiB headroom
    assert result.rule is memory_budget.BudgetRule.NO_LIMIT
    assert result.limit_bytes is None
    assert result.installed_bytes == 16 * GIB


def test_compute_measures_every_reserve_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{MODULE}.cgroup_memory_limit_bytes", lambda: 32 * GIB)
    proc = make_proc(tmp_path, dashboard=True)
    # The image's Claude: comm "claude", active binary bin/claude.exe.
    add_process(
        proc, 4_200_001, comm="claude", exe="/opt/claude-code/bin/claude.exe", rss_kib=1024
    )
    add_process(proc, 4_200_002, exe="/opt/bin/codex-x86_64-unknown-linux-musl", rss_kib=2048)
    python = partial(add_process, proc, comm="python", exe="/usr/bin/python3")
    python(4_200_003, argv=("python", "-m", "booley.mcp.server"), rss_kib=4096)
    add_process(
        proc,
        4_200_004,
        comm="booley-mcp",
        exe="/usr/bin/python3",
        argv=("/usr/bin/python3", "/usr/local/bin/booley-mcp"),
        rss_kib=8192,
    )
    dashboard = ("python", "-m", "booley.harness.dashboard")
    python(4_200_005, argv=dashboard, rss_kib=16384, sockets=("1", SOCKET_INODE))
    python(4_200_006, argv=("python3", "x.py"), rss_kib=99999)
    # T3 Code's server; a claude it started and the shared codex app-server are agents too.
    add_process(
        proc, 4_200_007, exe="/opt/t3code/t3", argv=("/opt/t3code/t3", "serve"), rss_kib=32768
    )
    add_process(proc, 4_200_008, comm="claude", ppid=4_200_007, rss_kib=64)
    add_process(
        proc,
        4_200_009,
        exe="/opt/bin/codex-x86_64-unknown-linux-musl",
        argv=("codex", "app-server"),
        ppid=4_200_007,
        rss_kib=128,
    )
    for pid, rss_kib in ((4_200_010, 256), (4_200_011, 512), (4_200_012, 1024)):
        add_process(proc, pid, exe="/opt/chrome/chrome-headless-shell", rss_kib=rss_kib)
    lookalikes = (
        ("/usr/bin/node", ("node", "serve.js")),
        ("/usr/bin/node", ("/usr/bin/node", "/srv/app/t3", "serve")),
        ("/opt/t3code/t3", ("t3", "project", "add", "/work")),
        ("/usr/lib/chromium/chromium", ("chromium", "--headless")),
        ("/opt/google/chrome/chrome", ("chrome", "--headless")),
    )
    for pid, (exe, argv) in enumerate(lookalikes, start=4_200_020):
        add_process(proc, pid, exe=exe, argv=argv, rss_kib=77777)

    result = memory_budget.compute_memory_budget(proc_root=proc)

    roles = memory_budget.ReserveRole
    kib = 1024
    assert [(p.role, p.pid, p.bytes) for p in result.reserve] == [
        (roles.DEVELOPER_AGENT, 4_200_001, 1024 * kib),
        (roles.DEVELOPER_AGENT, 4_200_002, 2048 * kib),
        (roles.DEVELOPER_AGENT, 4_200_008, 64 * kib),
        (roles.DEVELOPER_AGENT, 4_200_009, 128 * kib),
        (roles.MCP_SERVER, 4_200_003, 4096 * kib),
        (roles.MCP_SERVER, 4_200_004, 8192 * kib),
        (roles.DASHBOARD, 4_200_005, 16384 * kib),
        (roles.T3_CODE, 4_200_007, 32768 * kib),
        (roles.HEADLESS_CHROME, None, (256 + 512 + 1024) * kib),
        (roles.HEADROOM, None, 8 * GIB),
    ]
    assert {p.source for p in result.reserve} == {
        memory_budget.ReserveSource.MEASURED,
        memory_budget.ReserveSource.FIXED,
    }
    assert result.rule is memory_budget.BudgetRule.LIMIT
    measured_kib = 1024 + 2048 + 64 + 128 + 4096 + 8192 + 16384 + 32768 + 256 + 512 + 1024
    assert result.bytes == 32 * GIB - 8 * GIB - measured_kib * kib


def test_compute_without_cgroup_limit_reads_memtotal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{MODULE}.cgroup_memory_limit_bytes", lambda: None)
    proc = make_proc(tmp_path)
    (proc / "meminfo").write_text("MemTotal:       16777216 kB\nMemFree: 1 kB\n", encoding="utf-8")

    result = memory_budget.compute_memory_budget(proc_root=proc)

    assert result.rule is memory_budget.BudgetRule.NO_LIMIT
    assert result.installed_bytes == 16 * GIB
    # No process is running: every role is absent, only the headroom is held back.
    assert {p.source for p in result.reserve[:-1]} == {memory_budget.ReserveSource.ABSENT}
    assert result.bytes == 12 * GIB


@pytest.mark.parametrize(
    ("limit", "cgroup_swap", "host_swap", "headroom", "swap_bytes"),
    [
        pytest.param(32 * GIB, 8 * GIB, 4 * GIB, False, 4 * GIB, id="swap"),
        pytest.param(32 * GIB, 0, 4 * GIB, True, 0, id="no-swap"),
        pytest.param(32 * GIB, 1 << 60, 0, True, 0, id="no-host-swap"),
        pytest.param(32 * GIB, None, 4 * GIB, True, 0, id="no-swap-accounting"),
        pytest.param(None, 8 * GIB, 16 * GIB, True, 0, id="no-limit"),
    ],
)
def test_compute_drops_headroom_only_with_a_swap_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    cgroup_swap: int | None,
    host_swap: int,
    headroom: bool,
    swap_bytes: int,
) -> None:
    monkeypatch.setattr(f"{MODULE}.cgroup_memory_limit_bytes", lambda: limit)
    monkeypatch.setattr(f"{MODULE}.cgroup_swap_limit_bytes", lambda _limit: cgroup_swap)
    proc = make_proc(tmp_path)
    (proc / "meminfo").write_text(
        f"MemTotal: {16 * GIB // 1024} kB\nSwapTotal: {host_swap // 1024} kB\n", encoding="utf-8"
    )

    result = memory_budget.compute_memory_budget(proc_root=proc)

    roles = [p.role for p in result.reserve]
    assert (memory_budget.ReserveRole.HEADROOM in roles) is headroom
    assert result.swap_bytes == swap_bytes
    base = limit or 16 * GIB
    assert result.bytes == base - (base // 4 if headroom else 0)


def test_unreadable_meminfo_gives_no_swap_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{MODULE}.cgroup_memory_limit_bytes", lambda: 32 * GIB)
    monkeypatch.setattr(f"{MODULE}.cgroup_swap_limit_bytes", lambda _limit: 8 * GIB)
    proc = make_proc(tmp_path)  # no meminfo file

    result = memory_budget.compute_memory_budget(proc_root=proc)

    assert result.swap_bytes == 0
    assert result.bytes == 24 * GIB


def test_compute_without_readable_memtotal_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{MODULE}.cgroup_memory_limit_bytes", lambda: None)
    proc = make_proc(tmp_path)

    # No meminfo file, no MemTotal line, then an unparseable one.
    for text in (None, "MemFree: 1 kB\n", "MemTotal: lots kB\n"):
        if text is not None:
            (proc / "meminfo").write_text(text, encoding="utf-8")
        with pytest.raises((OSError, ValueError), match="meminfo"):
            memory_budget.compute_memory_budget(proc_root=proc)


def test_unreadable_process_counts_one_gib(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    add_process(
        proc, 4_200_001, exe="/usr/bin/python3", argv=("python", "-m", "booley.mcp.server")
    )

    add_process(proc, 4_200_002, exe="/opt/chrome/chrome-headless-shell", rss_kib=2048)
    add_process(proc, 4_200_003, exe="/opt/chrome/chrome-headless-shell")

    parts = {p.role: p for p in memory_budget.measure_reserve(proc_root=proc)}

    chrome = parts[memory_budget.ReserveRole.HEADLESS_CHROME]
    assert (chrome.bytes, chrome.source, chrome.pid) == (
        2048 * 1024 + GIB,
        memory_budget.ReserveSource.UNREADABLE,
        None,
    )
    server = parts[memory_budget.ReserveRole.MCP_SERVER]
    assert (server.bytes, server.source, server.pid) == (
        GIB,
        memory_budget.ReserveSource.UNREADABLE,
        4_200_001,
    )
    # Contrast: a role with no process is absent and counts nothing.
    dashboard = parts[memory_budget.ReserveRole.DASHBOARD]
    assert (dashboard.bytes, dashboard.source, dashboard.pid) == (
        0,
        memory_budget.ReserveSource.ABSENT,
        None,
    )


def test_process_without_vmrss_line_counts_one_gib(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    add_process(proc, 4_200_001, comm="claude", rss_kib=1)
    (proc / "4200001/status").write_text("Name:\tclaude\nVmRSS:\tlots kB\n", encoding="utf-8")

    (agent, *_rest) = memory_budget.measure_reserve(proc_root=proc)

    assert (agent.source, agent.bytes) == (memory_budget.ReserveSource.UNREADABLE, GIB)


def test_reused_pid_counts_one_gib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = make_proc(tmp_path)
    add_process(proc, 4_200_001, comm="claude", rss_kib=4096)
    real_read = memory_budget._read_rss_bytes

    def read_then_reuse(proc_root: Path, pid: int) -> int | None:
        rss = real_read(proc_root, pid)
        fields = ["S", "1", *["0"] * 17, "8", "0"]  # a new process took the PID
        (proc_root / str(pid) / "stat").write_text(
            f"{pid} (x) " + " ".join(fields), encoding="utf-8"
        )
        return rss

    monkeypatch.setattr(f"{MODULE}._read_rss_bytes", read_then_reuse)

    (agent, *_rest) = memory_budget.measure_reserve(proc_root=proc)

    assert (agent.source, agent.bytes, agent.pid) == (
        memory_budget.ReserveSource.UNREADABLE,
        GIB,
        4_200_001,
    )


def test_scan_skips_foreign_and_unreadable_entries(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    (proc / "4200010").mkdir()  # no exe, comm, cmdline, fd: nothing to classify or hold
    (proc / "0").mkdir()
    add_process(proc, 4_200_011, exe="/usr/bin/python3 (deleted)", argv=("python", "-m", "other"))

    assert {p.source for p in memory_budget.measure_reserve(proc_root=proc)} == {
        memory_budget.ReserveSource.ABSENT
    }


def test_missing_proc_root_reports_every_role_absent(tmp_path: Path) -> None:
    parts = memory_budget.measure_reserve(proc_root=tmp_path / "missing")

    assert [(p.role, p.source) for p in parts] == [
        (memory_budget.ReserveRole.DEVELOPER_AGENT, memory_budget.ReserveSource.ABSENT),
        (memory_budget.ReserveRole.MCP_SERVER, memory_budget.ReserveSource.ABSENT),
        (memory_budget.ReserveRole.DASHBOARD, memory_budget.ReserveSource.ABSENT),
        (memory_budget.ReserveRole.T3_CODE, memory_budget.ReserveSource.ABSENT),
        (memory_budget.ReserveRole.HEADLESS_CHROME, memory_budget.ReserveSource.ABSENT),
    ]


def test_dashboard_socket_prefix_matches_view_probe() -> None:
    from booley.harness.dashboard import view_probe

    assert view_probe.socket_name(SCOPE).replace("\0", "@") == (
        memory_budget.DASHBOARD_SOCKET_PREFIX + SCOPE
    )
