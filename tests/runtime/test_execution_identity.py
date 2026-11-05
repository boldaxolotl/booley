"""Booley build identity and host facts: what changes them, what refuses them."""

from __future__ import annotations

import csv
import importlib
import os
import shutil
import sys
import sysconfig
import types
from collections.abc import Callable, Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_POSIX = pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlinks, modes or FIFOs")
_POSIX_HOST = pytest.mark.skipif(os.name == "nt", reason="POSIX host facts: resource, uname")
_NON_ROOT = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions that root ignores"
)
_LIVE = object()  # a refusal row's request to run the search_path=None branch
_ORIG, _NEW = "orig", "ORIG"  # same size: only content differs


def _ident() -> Any:
    """Import the module under test inside the test body (red = call-phase failure)."""
    return importlib.import_module("booley.runtime.execution_identity")


def _write(path: Path, text: str = _ORIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _install(
    site: Path, name: str, files: tuple[str, ...], unrecorded: tuple[str, ...] = ()
) -> None:
    """Install ``files`` with a dist-info whose RECORD lists them (not ``unrecorded``)."""
    for rel in (*files, *unrecorded):
        _write(site / rel)
    dist = site / f"{name}-1.0.dist-info"
    _write(dist / "METADATA")
    record = (*files, f"{dist.name}/METADATA", f"{dist.name}/RECORD")
    _write(dist / "RECORD", "".join(f"{rel},sha256=x,1\n" for rel in record))


def _fake_site(tmp_path: Path) -> Path:
    """A root holding ``site/`` (booley, fusesoc, edalize) and ``bin/fusesoc``."""
    root = tmp_path / "root"
    site = root / "site"
    booley_files = (
        "booley/__init__.py",
        "booley/data/x.json",
        "booley/data/flow.tcl",
        "booley/fusesoc/x.py",  # a "contains fusesoc/" matcher would claim this
        "booley/run.sh",
    )
    _install(site, "booley", booley_files, ("booley/__pycache__/m.cpython-314.pyc",))
    (site / "booley/run.sh").chmod(0o755)
    _install(site, "fusesoc", ("fusesoc/main.py", "../bin/fusesoc"))
    _write(site / "fusesoc-1.0.dist-info/entry_points.txt")  # unrecorded metadata
    _install(site, "edalize", ("edalize/tools/base.py",), ("edalize/tools/extra.py",))
    return root


def _identity(ident: Any, root: Path, search: Any = None, *, live: bool = False) -> str:
    """Identity of the fixture; ``live`` runs the ``search_path=None`` branch."""
    importlib.invalidate_caches()
    return ident.booley_build_identity(None if live else search or [str(root / "site")])


def _site_op(op: Callable[[Path], object]) -> Callable[[Path], object]:
    """Row helper: run ``op`` on the fixture's ``site`` directory."""
    return lambda root: op(root / "site")


def _rewrite(rel: str) -> Callable[[Path], object]:
    return lambda root: _write(root / rel, _NEW)


def _symlink(root: Path, link: str, target: str | Path) -> None:
    (root / link).symlink_to(target)


def _repoint(link: str, target: str) -> Callable[[Path], None]:
    def repoint(root: Path) -> None:
        (root / link).unlink()
        _symlink(root, link, target)

    return repoint


def _outside_target(root: Path) -> None:
    _write(root / "outside/y.json")
    _symlink(root, "site/booley/data/link.json", root / "outside/y.json")


def _identical_targets(root: Path) -> None:
    for name in ("y.json", "z.json"):
        _write(root / "site/booley/data" / name)
    _symlink(root, "site/booley/data/link.json", "y.json")


def _boundary_setup(root: Path) -> None:
    for name in ("linkx", "x"):
        _write(root / "site/booley/data" / name)
    _symlink(root, "site/booley/data/a", "linkx")


_CHANGES = [
    pytest.param(None, _rewrite("site/booley/data/x.json"), id="data"),
    pytest.param(None, _rewrite("site/booley/data/flow.tcl"), id="tcl"),
    pytest.param(
        None, _rewrite("site/booley/__pycache__/m.cpython-314.pyc"), id="unrecorded-pycache"
    ),
    pytest.param(None, _rewrite("site/edalize/tools/extra.py"), id="unrecorded-namespace-module"),
    pytest.param(None, _rewrite("bin/fusesoc"), id="console-script"),
    pytest.param(None, _rewrite("site/fusesoc/new.py"), id="file-added"),
    pytest.param(
        None,
        lambda r: (r / "site/edalize/tools/extra.py").rename(r / "site/edalize/tools/other.py"),
        id="file-renamed",
    ),
    pytest.param(None, lambda r: (r / "site/edalize/tools/x").mkdir(), id="empty-dir-added"),
    pytest.param(
        None, _rewrite("site/fusesoc-1.0.dist-info/entry_points.txt"), id="unrecorded-metadata"
    ),
    pytest.param(
        None, lambda r: (r / "site/booley/run.sh").chmod(0o644), id="exec-bit", marks=_POSIX
    ),
    pytest.param(_outside_target, _rewrite("outside/y.json"), id="symlink-target", marks=_POSIX),
    pytest.param(
        _identical_targets,
        _repoint("site/booley/data/link.json", "z.json"),
        id="symlink-text",
        marks=_POSIX,
    ),
    pytest.param(
        _boundary_setup,
        lambda r: (
            (r / "site/booley/data/a").unlink(),
            _symlink(r, "site/booley/data/alink", "x"),
        ),
        id="link-field-boundary",
        marks=_POSIX,
    ),
]


@pytest.mark.parametrize(("setup", "change"), _CHANGES)
def test_build_identity_changes_with_bundled_file(
    tmp_path: Path, setup: Callable[[Path], None] | None, change: Callable[[Path], object]
) -> None:
    ident = _ident()
    root = _fake_site(tmp_path)
    if setup:
        setup(root)
    before = _identity(ident, root)
    change(root)
    assert _identity(ident, root) != before


def test_build_identity_live_import_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = _ident()
    site = str(_fake_site(tmp_path) / "site")
    stdlib = [sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
    for name in ident.BUILD_DISTRIBUTIONS:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "path", [site, *stdlib, str(Path(stdlib[1]) / "lib-dynload")])
    importlib.invalidate_caches()
    assert ident.booley_build_identity() == ident.booley_build_identity([site])


def _other_dist_info(site: Path, name: str = "other-1.0.dist-info") -> Path:
    return _write(site / name / "RECORD", "")


def _second_portion(root: Path, dist_info: bool) -> list[str]:
    """A second import-path directory with an ``edalize/`` portion."""
    other = root / "other"
    _write(other / "edalize/y.py")
    if dist_info:
        _write(other / "edalize-2.dist-info/RECORD", "edalize/y.py,,\n")
    return [str(root / "site"), str(other)]


def _only_search(root: Path, name: str, text: str | None = None) -> list[str]:
    """A search path whose only ``booley`` is a module, or nothing at all."""
    directory = root / name
    directory.mkdir()
    if text is not None:
        _write(directory / "booley.py", text)
    return [str(directory)]


def _append_record(text: str) -> Callable[[Path], None]:
    def append(root: Path) -> None:
        with (root / "site/booley-1.0.dist-info/RECORD").open("a", encoding="utf-8") as record:
            record.write(text)

    return append


def _chmod_zero(rel: str) -> Callable[[Path], None]:
    return lambda root: (root / rel).chmod(0)


def _unreadable_record(root: Path) -> None:
    _other_dist_info(root / "site").chmod(0)


def _stale_dist_info(root: Path) -> None:
    shutil.copytree(root / "site/edalize-1.0.dist-info", root / "site/edalize-0.9.dist-info")


def _egg_info_only(root: Path) -> None:
    shutil.rmtree(root / "site/booley-1.0.dist-info")
    _write(root / "site/booley-1.0.egg-info/SOURCES.txt", "booley/__init__.py\n")


_REFUSALS = [
    (
        "no-distribution",
        _site_op(lambda s: shutil.rmtree(s / "booley-1.0.dist-info")),
        "exactly one owning",
        (),
    ),
    ("two-distributions", _stale_dist_info, "exactly one owning", ()),
    ("foreign-namespace-portion", lambda r: _second_portion(r, True), "exactly one owning", ()),
    ("unowned-namespace-portion", lambda r: _second_portion(r, False), "portion outside", ()),
    ("egg-info-only", _egg_info_only, "exactly one owning", ()),
    ("unreadable-record", _unreadable_record, "cannot read", (_NON_ROOT,)),
    (
        "missing-record-entry",
        lambda r: (r / "site/booley/data/x.json").unlink(),
        "cannot hash booley",
        (),
    ),
    ("record-entry-directory", _append_record("booley/data,,\n"), "not a regular file", ()),
    (
        "directory-symlink",
        lambda r: (r / "site/booley/linkdir").symlink_to(r / "site/booley/data", True),
        "not a regular file",
        (_POSIX,),
    ),
    ("unlistable-directory", _chmod_zero("site/booley/data"), "cannot hash booley", (_NON_ROOT,)),
    (
        "unreadable-file",
        _chmod_zero("site/booley/data/x.json"),
        "cannot hash booley",
        (_NON_ROOT,),
    ),
    ("fifo", lambda r: os.mkfifo(r / "site/booley/pipe"), "not a regular file", (_POSIX,)),
    (
        "dangling-symlink",
        lambda r: _symlink(r, "site/booley/data/dead", "nowhere"),
        "cannot hash booley",
        (_POSIX,),
    ),
    (
        "module-not-package",
        lambda r: _only_search(r, "mods", _ORIG),
        "not an importable package",
        (),
    ),
    ("no-spec", lambda r: _only_search(r, "empty"), "not an importable package", ()),
    (
        "non-utf8-record",
        lambda r: (r / "site/booley-1.0.dist-info/RECORD").write_bytes(b"\xff\xfe,,\n"),
        "cannot read",
        (),
    ),
    (
        "record-csv-error",
        lambda r: _write(
            r / "site/booley-1.0.dist-info/RECORD", "x" * (csv.field_size_limit() + 1) + ",,\n"
        ),
        "cannot read",
        (),
    ),
    ("missing-record", lambda r: (r / "site/other-1.0.dist-info").mkdir(), "cannot read", ()),
    ("spec-none", lambda r: _LIVE, "cannot hash booley", ()),
    ("record-row-without-path", _append_record(",sha256=x,12\n"), "without a path", ()),
]


@pytest.mark.parametrize(
    ("break_it", "match"),
    [pytest.param(fn, match, id=case, marks=marks) for case, fn, match, marks in _REFUSALS],
)
def test_build_identity_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, break_it: Callable[[Path], Any], match: str
) -> None:
    ident = _ident()
    root = _fake_site(tmp_path)
    search = break_it(root)
    live = search is _LIVE
    search = search if isinstance(search, list) else None  # edits return junk
    if live:  # a module whose __spec__ is None makes find_spec raise ValueError
        monkeypatch.setitem(sys.modules, "booley", types.ModuleType("booley"))
    with pytest.raises(ident.IdentityError, match=match):
        _identity(ident, root, search, live=live)


@pytest.mark.parametrize(
    "with_symlink", [False, pytest.param(True, marks=_POSIX)], ids=["plain", "with-symlink"]
)
def test_build_identity_is_content_only(tmp_path: Path, with_symlink: bool) -> None:
    ident = _ident()
    root = _fake_site(tmp_path)
    if with_symlink:
        _symlink(root, "site/booley/data/link.json", "x.json")
    before = _identity(ident, root)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy, symlinks=True, copy_function=shutil.copy)
    follow = {"follow_symlinks": False} if os.utime in os.supports_follow_symlinks else {}
    for directory, dir_names, file_names in os.walk(copy):
        for name in (*dir_names, *file_names, ""):
            os.utime(Path(directory, name), (1_000_000, 1_000_000), **follow)
    assert _identity(ident, copy) == before


# --- host facts ------------------------------------------------------------


def _cpu_block(cpu: int, model: str, flags: str) -> str:
    return (
        f"processor\t: {cpu}\nvendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 151\n"
        f"model name\t: {model}\nstepping\t: 2\ncpu MHz\t\t: {1000 + cpu}\ncore id\t\t: {cpu}\n"
        f"flags\t\t: {flags}\n"
    )


def _x86_kept(model: str, flags: str) -> tuple[tuple[str, str], ...]:
    return (
        ("cpu family", "6"),
        ("flags", flags),
        ("model", "151"),
        ("model name", model),
        ("stepping", "2"),
        ("vendor_id", "GenuineIntel"),
    )


_CPUINFO = "\n".join(
    _cpu_block(cpu, *("P-core", "fpu avx512f") if cpu < 2 else ("E-core", "fpu avx2"))
    for cpu in range(4)
)
_MOUNT = "30 25 0:26 / /sys/fs/cgroup ro,nosuid{optional} - cgroup2 cgroup ro\n"
_MOUNTINFO = "25 1 8:1 / / rw - ext4 /dev/sda1 rw\n" + _MOUNT.format(optional="")
_LIMITS = {
    "cgroup.controllers": "cpu memory pids\n",
    "cpu.max": "max 100000\n",
    "memory.max": "max\n",
    "memory.high": "max\n",
    "memory.swap.max": "max\n",
    "pids.max": "4096\n",
}


def _fake_host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    _write(root / "proc/cpuinfo", _CPUINFO)
    _write(root / "proc/meminfo", "MemTotal:       16000000 kB\nSwapTotal:       2000000 kB\n")
    _write(root / "proc/self/cgroup", "0::/\n")
    _write(root / "proc/self/mountinfo", _MOUNTINFO)
    for name, text in _LIMITS.items():
        _write(root / "sys/fs/cgroup" / name, text)
    return root


def _facts(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    affinity: Collection[int] | None = (0, 1),
    rlimit: tuple[int, int] = (10, 20),
    release: str = "6.1.0",
) -> Any:
    """Collect host facts with the process-level inputs pinned."""
    import resource

    ident = _ident()
    if affinity is None:
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    else:
        monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(affinity), raising=False)
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(release=release), raising=False)
    last = sorted(n for n in dir(resource) if n.startswith("RLIMIT_"))[-1]
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda which: rlimit if which == getattr(resource, last) else (10, 20),
    )
    return ident.collect_host_facts(root)


def _replace(rel: str, old: str, new: str) -> Callable[[Path], None]:
    return lambda root: (root / rel).write_text((root / rel).read_text().replace(old, new))


def _set(rel: str, text: str) -> Callable[[Path], object]:
    return lambda root: _write(root / rel, text)


def _cgroup_file(name: str, text: str) -> Callable[[Path], None]:
    return _set(f"sys/fs/cgroup/{name}", text)


_MEMINFO = "proc/meminfo"
_DIGEST_CHANGES = [
    pytest.param({}, None, {"affinity": {0}}, id="affinity"),
    pytest.param({}, _cgroup_file("cpu.max", "200000 100000\n"), {}, id="cgroup-cpu-max"),
    pytest.param({"affinity": {0}}, None, {"affinity": {2}}, id="affinity-cores"),
    pytest.param({"affinity": {0, 1, 2}}, None, {"affinity": {0, 2, 3}}, id="core-mix"),
    pytest.param({}, _cgroup_file("memory.max", "1000\n"), {}, id="memory-max"),
    pytest.param({}, _cgroup_file("memory.high", "1000\n"), {}, id="memory-high"),
    pytest.param({}, _cgroup_file("memory.swap.max", "0\n"), {}, id="memory-swap-max"),
    pytest.param({}, _cgroup_file("pids.max", "100\n"), {}, id="pids-max"),
    pytest.param({}, None, {"rlimit": (11, 20)}, id="rlimit-soft"),
    pytest.param({}, None, {"rlimit": (10, 21)}, id="rlimit-hard"),
    pytest.param({}, _replace("proc/cpuinfo", "avx512f", "avx512f avx512bw"), {}, id="cpu-flags"),
    pytest.param({}, None, {"release": "6.2.0"}, id="kernel-release"),
    pytest.param({}, _replace(_MEMINFO, "16000000", "32000000"), {}, id="memtotal"),
    pytest.param({}, _replace(_MEMINFO, "2000000", "4000000"), {}, id="swaptotal"),
]


@_POSIX_HOST
@pytest.mark.parametrize(("base", "edit", "changed"), _DIGEST_CHANGES)
def test_host_facts_digest_changes_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: dict[str, Any],
    edit: Callable[[Path], None] | None,
    changed: dict[str, Any],
) -> None:
    root = _fake_host(tmp_path)
    before = _facts(root, monkeypatch, **base).digest()
    if edit:
        edit(root)
    assert _facts(root, monkeypatch, **changed).digest() != before


@_POSIX_HOST
def test_host_facts_digest_is_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    root = _fake_host(tmp_path)
    facts = _facts(root, monkeypatch)
    assert facts.digest() == _facts(root, monkeypatch).digest()
    assert [name for name, _, _ in facts.rlimits] == sorted(
        n for n in dir(resource) if n.startswith("RLIMIT_")
    )


@_POSIX_HOST
def test_host_facts_cpu_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _facts(_fake_host(tmp_path), monkeypatch, affinity={0, 1, 2, 3})
    assert facts.cpu == (
        (_x86_kept("E-core", "fpu avx2"), 2),
        (_x86_kept("P-core", "fpu avx512f"), 2),
    )


@_POSIX_HOST
def test_host_facts_record_aarch64_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_host(tmp_path)
    _write(
        root / "proc/cpuinfo",
        "processor\t: 0\nBogoMIPS\t: 50.00\nFeatures\t: fp asimd\nCPU implementer\t: 0x41\n"
        "CPU architecture: 8\nCPU variant\t: 0x3\nCPU part\t: 0xd0c\nCPU revision\t: 1\n\n"
        "Hardware\t: BCM2835\nRevision\t: c03111\nSerial\t: 0000\n",
    )
    assert _facts(root, monkeypatch, affinity={0}).cpu == (
        (
            (
                ("CPU architecture", "8"),
                ("CPU implementer", "0x41"),
                ("CPU part", "0xd0c"),
                ("CPU revision", "1"),
                ("CPU variant", "0x3"),
                ("Features", "fp asimd"),
            ),
            1,
        ),
    )


@_POSIX_HOST
def test_cgroup_levels_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_host(tmp_path)
    cgroup = root / "sys/fs/cgroup"
    shutil.rmtree(cgroup)
    _write(root / "proc/self/cgroup", "0::/outer/inner\n")
    _write(root / "proc/self/mountinfo", _MOUNT.format(optional=" shared:5"))
    _write(cgroup / "cgroup.controllers", "cpu memory\n")  # top: no cpu.max, no memory.high
    _write(cgroup / "memory.max", "7777\n")
    outer = cgroup / "outer"
    _write(outer / "cgroup.controllers", "cpu memory pids\n")
    for name, text in {
        "cpu.max": "50000 100000",
        "memory.max": "2000",
        "memory.high": "1500",
        "pids.max": "100",
    }.items():
        _write(outer / name, text + "\n")
    _write(outer / "inner/cgroup.controllers", "cpu\n")
    _write(outer / "inner/cpu.max", "max 100000\n")
    absent = "absent"
    assert _facts(root, monkeypatch).cgroup == (
        (
            ("cpu.max", "max 100000"),
            ("memory.max", absent),
            ("memory.high", absent),
            ("pids.max", absent),
            ("memory.swap.max", absent),
        ),
        (
            ("cpu.max", "50000 100000"),
            ("memory.max", "2000"),
            ("memory.high", "1500"),
            ("pids.max", "100"),
            ("memory.swap.max", absent),
        ),
        (
            ("cpu.max", "root"),
            ("memory.max", "7777"),
            ("memory.high", "root"),
            ("pids.max", absent),
            ("memory.swap.max", absent),
        ),
    )


def _leaf_cgroup(root: Path, controllers: str | None, files: tuple[str, ...]) -> None:
    """Point the process at ``/leaf`` holding ``files`` (and maybe controllers)."""
    _write(root / "proc/self/cgroup", "0::/leaf\n")
    leaf = root / "sys/fs/cgroup/leaf"
    for name in files:
        _write(leaf / name, "max\n")
    if controllers is not None:
        _write(leaf / "cgroup.controllers", controllers)


def _directory_for(rel: str) -> Callable[[Path], None]:
    def swap(root: Path) -> None:
        (root / rel).unlink()
        (root / rel).mkdir()

    return swap


_STRIPPED_E_CORE = "processor\t: 2\ncpu MHz\t\t: 1\n"
_HOST_REFUSALS = [
    (
        "cgroup-mount-not-namespace-root",
        _replace("proc/self/mountinfo", "0:26 /", "0:26 /docker/x"),
        {},
        "cgroup2 mount rooted",
    ),
    (
        "two-cgroup-mounts",
        _replace(
            "proc/self/mountinfo",
            "rw\n3",
            "rw\n31 30 0:27 /.. /sys/fs/cgroup ro - cgroup2 cgroup ro\n3",
        ),
        {},
        "expected one",
    ),
    (
        "no-cgroup-mount",
        _set("proc/self/mountinfo", "25 1 8:1 / / rw - ext4 /dev/sda1 rw\n"),
        {},
        "expected one",
    ),
    ("cgroup-v1-line", _set("proc/self/cgroup", "4:cpu:/\n"), {}, "pure cgroup v2"),
    ("hybrid-lines", _set("proc/self/cgroup", "0::/\n4:cpu:/\n"), {}, "pure cgroup v2"),
    ("dotdot-path", _set("proc/self/cgroup", "0::/a/../b\n"), {}, r"contains '\.\.'"),
    ("missing-leaf", _set("proc/self/cgroup", "0::/nope\n"), {}, "is missing"),
    ("leaf-without-controllers", lambda r: _leaf_cgroup(r, None, ("cpu.max",)), {}, "is missing"),
    ("missing-cpu-max", lambda r: _leaf_cgroup(r, "cpu\n", ()), {}, "cpu.max is missing"),
    (
        "no-cpu-identity-keys",
        _replace("proc/cpuinfo", _cpu_block(2, "E-core", "fpu avx2"), _STRIPPED_E_CORE),
        {"affinity": {0, 2}},
        "no usable block",
    ),
    ("affinity-cpu-without-block", None, {"affinity": {0, 9}}, "no usable block"),
    ("no-affinity-api", None, {"affinity": None}, "no CPU affinity API"),
    ("no-memtotal", _set(_MEMINFO, "SwapTotal: 1 kB\n"), {}, "no MemTotal"),
    ("no-swaptotal", _set(_MEMINFO, "MemTotal: 1 kB\n"), {}, "no SwapTotal"),
    ("top-cpu-max-unreadable", _directory_for("sys/fs/cgroup/cpu.max"), {}, "Is a directory"),
    ("swap-max-unreadable", _directory_for("sys/fs/cgroup/memory.swap.max"), {}, "Is a directory"),
    (
        "non-utf8-cpuinfo",
        lambda r: (r / "proc/cpuinfo").write_bytes(b"\xff\xfe"),
        {},
        "cannot collect host facts",
    ),
    (
        "bad-memtotal",
        _set(_MEMINFO, "MemTotal: abc kB\nSwapTotal: 1 kB\n"),
        {},
        "cannot collect host facts",
    ),
]


@_POSIX_HOST
@pytest.mark.parametrize(
    ("edit", "kwargs", "match"),
    [pytest.param(*row[1:], id=row[0]) for row in _HOST_REFUSALS],
)
def test_host_facts_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Path], None] | None,
    kwargs: dict[str, Any],
    match: str,
) -> None:
    root = _fake_host(tmp_path)
    if edit:
        edit(root)
    with pytest.raises(_ident().IdentityError, match=match):
        _facts(root, monkeypatch, **kwargs)
