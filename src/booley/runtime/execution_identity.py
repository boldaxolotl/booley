"""Booley build identity and host facts: two key parts of the Result Cache.

ADR 0076 section 2 keys a cached result by, among other parts, the Booley code
that produced it and the host that ran it. This module computes both as
``sha256:<hex>`` strings and refuses (``IdentityError``) whenever it cannot
account for every input, so a consumer reports "unavailable" instead of
serving a result under a partial identity.

``booley_build_identity`` is the installed-content identity of the ``booley``,
``fusesoc`` and ``edalize`` distributions. It is not the image identity of
``booley.runtime.image_identity`` (``BooleyBuildIdentity`` there describes a
Sandbox Image). Everything else on the import path (Jinja2, PyYAML,
entry-point plugins of other distributions, ``sys.pycache_prefix``
redirection, the interpreter) belongs to the ownership and import-path gate of
the availability check and to the image ID.

Reusable by Goal evidence freshness (#2052) as well as the Result Cache.

Contracts consumers must honour:

- The identity hashes on-disk content at call time, not the bytes a long-lived
  process already loaded. A consumer captures it at process start and requires
  the value to equal the one at both key snapshots (ADR 0076 section 1), so an
  install replaced under a running process never stores or serves.
- It describes the calling interpreter's import view. A subprocess (``fusesoc``
  or ``python3`` from ``PATH``, a redirected bytecode prefix) is checked
  elsewhere, and so are modules imported from a Project directory that was
  temporarily on ``sys.path``.
- Content only is hashed. Which of a hashed ``.pyc`` and its hashed source
  runs is not keyed (CPython decides it from the ``.pyc`` header, and
  mtime and size are not hashed); switching takes a write to the install,
  which the ownership rule refuses. A redirected bytecode prefix lies
  outside the install, so that rule does not cover it; its check is
  the availability gate's (#2210).
- ``collect_host_facts`` describes the calling process at call time (affinity
  is the calling thread's; rlimits and cgroup are the caller's), so collect
  them in the process that launches the tools, at both key snapshots.
- Accepted residual: CPU, memory or pids limits set on cgroups above the
  Sandbox's private cgroup namespace (``--cgroup-parent``, a systemd slice
  quota) cannot be read from inside it.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import posixpath
import re
import stat
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

BUILD_DISTRIBUTIONS: tuple[str, ...] = ("booley", "fusesoc", "edalize")
_FORMAT_TAG = b"booley-build-identity/1"
_CHUNK_BYTES = 1 << 20

# cpuinfo keys that identify the CPU model (x86 and aarch64); clocks,
# microcode and per-core ids vary without changing what the CPU computes.
_CPU_KEYS = frozenset(
    {
        "vendor_id",
        "cpu family",
        "model",
        "model name",
        "stepping",
        "flags",
        "Features",
        "CPU implementer",
        "CPU architecture",
        "CPU variant",
        "CPU part",
        "CPU revision",
    }
)
_CPU_IDENTITY_KEYS = ("model name", "flags", "Features", "CPU part")
# (file, controller): the cgroup v2 limit files read at every level.
_CGROUP_LIMITS = (
    ("cpu.max", "cpu"),
    ("memory.max", "memory"),
    ("memory.high", "memory"),
    ("pids.max", "pids"),
    ("memory.swap.max", "memory"),
)


_CpuFacts = tuple[tuple[tuple[tuple[str, str], ...], int], ...]


class IdentityError(RuntimeError):
    """The identity or the host facts cannot be established."""


# --- Booley build identity -------------------------------------------------


def booley_build_identity(search_path: Sequence[str] | None = None) -> str:
    """Return ``sha256:<hex>`` over the installed files of ``BUILD_DISTRIBUTIONS``.

    ``search_path`` is a test seam: ``None`` asks the interpreter where it
    imports each package from, a sequence replaces ``sys.path`` for the lookup.
    Raises ``IdentityError`` on development installs, ambiguous or foreign
    ownership, and any file that cannot be read as a regular file.
    """
    digest = hashlib.sha256(_frame(_FORMAT_TAG))
    for name in BUILD_DISTRIBUTIONS:
        try:
            entries, record_keys = _distribution_entries(name, search_path)
            digest.update(_frame(name.encode()) + len(entries).to_bytes(8, "big"))
            for key, path in sorted(entries.items()):
                digest.update(_encode_entry(key, path, key in record_keys))
        except (OSError, UnicodeError, ValueError) as error:
            raise IdentityError(f"cannot hash {name}: {error}") from error
    return "sha256:" + digest.hexdigest()


def _frame(*fields: bytes) -> bytes:
    """Length-prefix each field so concatenations cannot be re-split."""
    return b"".join(len(field).to_bytes(8, "big") + field for field in fields)


def _package_locations(name: str, search_path: Sequence[str] | None) -> list[Path]:
    if search_path is None:
        spec = importlib.util.find_spec(name)
    else:
        spec = importlib.machinery.PathFinder.find_spec(name, list(search_path))
    if spec is None or spec.submodule_search_locations is None:
        raise IdentityError(f"{name} is not an importable package")
    return [Path(location) for location in spec.submodule_search_locations]


def _record_paths(dist_info: Path) -> list[str]:
    record_file = dist_info / "RECORD"
    try:
        with record_file.open(newline="", encoding="utf-8") as record:
            rows = [row for row in csv.reader(record) if row]
    except (OSError, UnicodeError, csv.Error) as error:
        raise IdentityError(f"cannot read {record_file}: {error}") from error
    if not all(row[0] for row in rows):
        raise IdentityError(f"{record_file} has a row without a path")
    return [posixpath.normpath(row[0]) for row in rows]


def _owning_distribution(name: str, locations: list[Path]) -> tuple[Path, list[str]]:
    """Return the one dist-info directory whose RECORD installs ``name/...``."""
    matches: dict[Path, list[str]] = {}
    for site_dir in dict.fromkeys(location.parent for location in locations):
        with os.scandir(site_dir) as listing:
            dist_infos = [Path(e.path) for e in listing if e.name.endswith(".dist-info")]
        for dist_info in dist_infos:
            paths = _record_paths(dist_info)
            if any(path.split("/")[0] == name for path in paths):
                matches[dist_info] = paths
    if len(matches) != 1:
        raise IdentityError(
            f"{name} needs exactly one owning distribution, found {sorted(matches)}"
        )
    ((dist_info, paths),) = matches.items()
    if {location.parent for location in locations} != {dist_info.parent}:
        raise IdentityError(f"{name} has a portion outside {dist_info.parent}")
    return dist_info, paths


def _raise(error: OSError) -> None:
    """``os.walk`` error hook: an unlistable directory must not vanish."""
    raise error


def _distribution_entries(
    name: str, search_path: Sequence[str] | None
) -> tuple[dict[str, Path], set[str]]:
    """Map file-set key -> path for one distribution, plus the RECORD-listed keys."""
    locations = _package_locations(name, search_path)
    dist_info, record = _owning_distribution(name, locations)
    site_dir = dist_info.parent
    entries = {rel: Path(os.path.normpath(site_dir / rel)) for rel in record}
    for root in (*locations, dist_info):
        for walked, dir_names, file_names in os.walk(root, onerror=_raise):
            directory = Path(walked)
            for entry in (directory, *(directory / n for n in (*dir_names, *file_names))):
                entries[entry.relative_to(site_dir).as_posix()] = entry
    return entries, set(record)


def _encode_entry(key: str, path: Path, from_record: bool) -> bytes:
    """Encode one file-set entry: key, kind, link text, exec bits, content digest."""
    info = path.lstat()
    kind, link, target = "file", "", info
    if stat.S_ISLNK(info.st_mode):
        kind, link, target = "link", str(path.readlink()), path.stat()
    elif stat.S_ISDIR(info.st_mode) and not from_record:
        return _frame(os.fsencode(key), b"dir", b"", b"0", b"")
    if not stat.S_ISREG(target.st_mode):
        raise IdentityError(f"{path} is not a regular file or a link to one")
    content = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            content.update(chunk)
    exec_bits = str(target.st_mode & 0o111).encode()
    return _frame(os.fsencode(key), kind.encode(), os.fsencode(link), exec_bits, content.digest())


# --- Host facts ------------------------------------------------------------


@dataclass(frozen=True)
class HostFacts:
    """What the host contributes to a result: CPU, memory, limits, kernel."""

    cpu: _CpuFacts
    kernel_release: str
    mem_total_kib: int
    swap_total_kib: int
    affinity_count: int
    cgroup: tuple[tuple[tuple[str, str], ...], ...]
    rlimits: tuple[tuple[str, int, int], ...]

    def digest(self) -> str:
        canonical = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def collect_host_facts(root: Path = Path("/")) -> HostFacts:
    """Read the host facts under ``root``; raise ``IdentityError`` if any is missing."""
    try:
        return _collect(root)
    except (OSError, UnicodeError, ValueError) as error:
        raise IdentityError(f"cannot collect host facts: {error}") from error


def _collect(root: Path) -> HostFacts:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is None:
        raise IdentityError("no CPU affinity API on this platform")
    affinity = get_affinity(0)
    cpu = _cpu_blocks(root, affinity)
    meminfo = (root / "proc/meminfo").read_text(encoding="utf-8")
    # Imported after the reads above so platforms without them refuse first.
    import resource

    rlimit_names = sorted(n for n in dir(resource) if n.startswith("RLIMIT_"))
    return HostFacts(
        cpu=cpu,
        kernel_release=os.uname().release,
        mem_total_kib=_meminfo_kib(meminfo, "MemTotal"),
        swap_total_kib=_meminfo_kib(meminfo, "SwapTotal"),
        affinity_count=len(affinity),
        cgroup=_cgroup_levels(root),
        rlimits=tuple((n, *resource.getrlimit(getattr(resource, n))) for n in rlimit_names),
    )


def _cpu_blocks(root: Path, affinity: set[int]) -> _CpuFacts:
    """Distinct reduced cpuinfo blocks of the affinity CPUs, each with a count."""
    text = (root / "proc/cpuinfo").read_text(encoding="utf-8")
    reduced: dict[int, tuple[tuple[str, str], ...]] = {}
    for block in re.split(r"\n\s*\n", text):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        if "processor" in fields:  # board blocks (Hardware, Serial) have none
            cpu_id = int(fields["processor"])
            reduced[cpu_id] = tuple(sorted((k, v) for k, v in fields.items() if k in _CPU_KEYS))
    counts: Counter[tuple[tuple[str, str], ...]] = Counter()
    for cpu_id in sorted(affinity):
        block = reduced.get(cpu_id)
        if block is None or not any(key in dict(block) for key in _CPU_IDENTITY_KEYS):
            raise IdentityError(f"cpuinfo has no usable block for CPU {cpu_id}")
        counts[block] += 1
    return tuple(sorted(counts.items()))


def _meminfo_kib(meminfo: str, key: str) -> int:
    for line in meminfo.splitlines():
        name, _, value = line.partition(":")
        if name == key:
            return int(value.replace("kB", "").strip())
    raise IdentityError(f"meminfo has no {key}")


def _require_cgroup2_namespace_root(root: Path) -> None:
    """Refuse unless the cgroup2 mount shows this process's own hierarchy root.

    In a cgroup namespace entered without remounting, ``0::/`` would make the
    walk read the true root and record nothing.
    """
    mountinfo = (root / "proc/self/mountinfo").read_text(encoding="utf-8")
    mounts = [
        fields
        for fields in (line.split() for line in mountinfo.splitlines())
        if len(fields) > 4 and fields[4] == "/sys/fs/cgroup"
    ]
    if len(mounts) != 1:
        raise IdentityError(f"expected one /sys/fs/cgroup mount, found {len(mounts)}")
    fields = mounts[0]
    fs_type = fields[fields.index("-", 6) + 1 :][:1]
    if fs_type != ["cgroup2"] or fields[3] != "/":
        raise IdentityError("/sys/fs/cgroup is not a cgroup2 mount rooted at /")


def _cgroup_levels(root: Path) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Limits at the process's cgroup and every ancestor up to the mount root."""
    lines = [
        line
        for line in (root / "proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1 or not lines[0].startswith("0::"):
        raise IdentityError("host is not a pure cgroup v2 hierarchy")
    parts = [part for part in lines[0][3:].split("/") if part]
    if ".." in parts:
        raise IdentityError("cgroup path contains '..'")
    _require_cgroup2_namespace_root(root)
    base = root / "sys/fs/cgroup"
    if not (base.joinpath(*parts) / "cgroup.controllers").is_file():
        raise IdentityError(f"cgroup directory for {'/'.join(parts)!r} is missing")
    return tuple(
        _cgroup_level(base.joinpath(*parts[:depth]), is_top=depth == 0)
        for depth in range(len(parts), -1, -1)
    )


def _cgroup_level(level: Path, *, is_top: bool) -> tuple[tuple[str, str], ...]:
    controllers = (level / "cgroup.controllers").read_text(encoding="utf-8").split()
    values = []
    for name, controller in _CGROUP_LIMITS:
        if controller not in controllers:
            value = "absent"  # no limit can apply at this level
        else:
            try:
                value = (level / name).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                if name == "memory.swap.max":
                    value = "absent"  # swap accounting is off
                elif is_top:
                    value = "root"  # the true hierarchy root has no limit files
                else:
                    raise IdentityError(f"{level / name} is missing") from None
        values.append((name, value))
    return tuple(values)
