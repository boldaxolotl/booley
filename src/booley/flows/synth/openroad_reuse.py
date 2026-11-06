"""Reuse the OpenROAD stage of physical synthesis when none of its inputs changed.

Yosys always runs. Then the **OpenROAD reuse key** (SHA-256 over every input
that can change OpenROAD's result) is compared with the Target's **reuse slot**,
the last run whose OpenROAD stage completed; on a match its outputs are reused.

Input closure. An input not proven irrelevant is keyed; one that cannot be
keyed soundly declines reuse (``collect_openroad_inputs`` returns ``None``).
Keyed, in order:

1. ``netlist``: ``sta_<top>.v`` raw bytes, never normalized.
2. ``script``: ``run_openroad.tcl``.
3. ``recipe``: the Makefile's OpenROAD recipe, attempt token fixed.
4. ``tech_lef``, ``stdcell_lef``, ``wire_rc``, ``liberty`` contents. A relative
   path declines (OpenROAD resolves it in the build dir); so does a liberty
   with ``include_file`` (it reads an unkeyed file).
5. ``sdc[i]``: the SDC copies configure stages in the build dir; OpenROAD reads
   those, so a Project SDC edit during the run cannot reach it. SDC and
   wire-RC are Tcl: accepted only under :func:`tcl_decline_reason`. For keyed
   files outside Booley's build dir (e.g. a writable ``PRJ_LIB_DIR``) the
   ``pre == post`` check is the only guard against an edit made and undone
   during one run.
6. ``openroad_identity``: resolved path, ``-version``, SHA-256 of the binary and
   of each library ``ldd`` resolves; a non-ELF ``openroad`` declines.
   ``dlopen``ed code (Tcl packages, Qt plugins) is covered by record 7.
7. ``image_identity``: the image's ``BOOLEY_*`` build values and dpkg inventory;
   outside a Sandbox or with an unknown build time, reuse declines.
8. ``booley_version``: version, revision, payload fingerprint.
9. ``cpu_identity``: first CPU's vendor, model, flags (shared checkouts).
10. ``environment``: names OpenROAD, Tcl, the loader, libc, Qt, Kokkos, OpenMP
    or make read. ``PYTHON*`` is keyed although the shipped build is
    ``-Python``. A set ``LD_PRELOAD``/``LD_AUDIT`` declines (unhashed code).

Records 6-9 are computed once per run (:func:`sandbox_identity`); 1-5 and 10
are re-read after OpenROAD so a change during the run stores nothing.

Not keyed: thread count (spike #2467: same output at 1-16 threads, no seed);
the absolute build dir (all script paths are cwd-relative); Yosys inputs (in
the netlist); ``HOME`` (init file only, skipped by ``-no_init``); and the
executor's ``TMPDIR``/``TMP``/``TEMP``, ``PYTHONPYCACHEPREFIX``,
``PYTEST_ADDOPTS`` and execution id, none of which OpenROAD reads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from booley.flows.synth.backends import pipeline
from booley.flows.synth.backends.openroad import timing as openroad_timing
from booley.runtime.build_metadata import current_build_metadata
from booley.runtime.execution_records import atomic_write_json

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_S = 10
_SLOT_SCHEMA = 1
_MANIFEST = "slot.json"

# fmt: off
_ENV_EXACT = frozenset({
    "PATH", "PRJ_LIB_DIR", "LANG", "LANGUAGE", "GLIBC_TUNABLES", "REPORTS_DIR",
    "BUILD_WORKSPACE_DIRECTORY", "ENABLE_GPU", "ENABLE_EXTRA_DPL", "MAKEFLAGS", "MAKEFILES",
    "GNUMAKEFLAGS", "MFLAGS",
})
_ENV_PREFIXES = (
    "LD_", "LC_", "TCL", "TK_", "RSZ_", "GPL_", "KOKKOS_", "OMP_", "QT_", "OPENROAD", "OR_",
    "PYTHON",
)
_IMAGE_ENV = (
    "BOOLEY_IMAGE_BUILT_AT", "BOOLEY_PAYLOAD_FINGERPRINT", "BOOLEY_SOURCE_REVISION",
    "BOOLEY_VERSION", "BOOLEY_SANDBOX_FLAVOR",
)
# Strict SDC / wire-RC line grammar: constraint commands with no file operand
# or I/O, and no Tcl builtins at all.
_SDC_COMMANDS = frozenset({
    "create_clock", "create_generated_clock", "set_input_delay", "set_output_delay",
    "set_false_path", "set_multicycle_path", "set_max_delay", "set_min_delay",
    "set_clock_groups", "set_clock_uncertainty", "set_clock_latency", "set_clock_transition",
    "set_propagated_clock", "set_load", "set_driving_cell", "set_input_transition",
    "set_max_fanout", "set_max_transition", "set_max_capacitance", "set_case_analysis",
    "set_disable_timing", "set_timing_derate", "set_ideal_network", "set_units", "group_path",
    "set_dont_touch", "set_layer_rc", "set_wire_rc",
})
_SDC_GETTERS = frozenset({
    "get_ports", "get_pins", "get_cells", "get_nets", "get_clocks", "all_inputs",
    "all_outputs", "all_clocks", "all_registers", "current_design",
})
# fmt: on
_WS = r"[ \t]"
_PLAIN = r"[A-Za-z0-9_.\-/*:+]+"
# Bus indices are legal only inside a getter's brace-list, where Tcl reads them literally.
_OBJECT = r"(?:[A-Za-z0-9_.\-/*:+]|\[[0-9*:]+\])+"
_BRACE_PLAIN = rf"\{{{_WS}*(?:{_PLAIN}(?:{_WS}+{_PLAIN})*)?{_WS}*\}}"
_BRACE_OBJECT = rf"\{{{_WS}*(?:{_OBJECT}(?:{_WS}+{_OBJECT})*)?{_WS}*\}}"
_GETTER = rf"\[{_WS}*[a-z_]+(?:{_WS}+(?:{_PLAIN}|{_BRACE_OBJECT}))*{_WS}*\]"
_COMMAND_LINE = re.compile(
    rf"{_WS}*(?P<cmd>{_PLAIN})(?:{_WS}+(?:{_GETTER}|{_BRACE_PLAIN}|{_PLAIN}))*{_WS}*"
)
_BRACKET_COMMAND = re.compile(rf"\[{_WS}*([a-z_]+)")


@dataclass(frozen=True)
class OpenRoadInputs:
    """Every input that can change OpenROAD's result, in key order."""

    files: tuple[tuple[str, Path], ...]
    texts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Producer:
    """The run whose OpenROAD outputs a slot holds."""

    run_id: str
    started_at: str  # UTC RFC 3339 with Z


@dataclass(frozen=True)
class SlotEntry:
    """A published slot: its producer and the generation directory holding its files."""

    producer: Producer
    generation: Path
    files: tuple[str, ...]


def openroad_reuse_key(inputs: OpenRoadInputs | None) -> str | None:
    """SHA-256 over a length-delimited encoding of *inputs*; ``None`` when unprovable."""
    if inputs is None:
        return None
    digest = hashlib.sha256()

    def record(role: str, value: bytes) -> None:
        name = role.encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(value).to_bytes(8, "big") + value)

    try:
        for role, path in inputs.files:
            record(role, path.read_bytes())
    except OSError:
        return None
    for role, text in inputs.texts:
        record(role, text.encode())
    return digest.hexdigest()


def collect_openroad_inputs(
    plan: pipeline.SynthPlan, identity: tuple[tuple[str, str], ...] | None
) -> OpenRoadInputs | None:
    """Gather the closure for one configured physical plan; ``None`` declines reuse.

    *identity* is :func:`sandbox_identity`, computed once per run.
    """
    if identity is None:
        return None
    spec = plan.spec
    pdk = openroad_timing.openroad_pdk_paths()
    pdk_files = (
        ("tech_lef", pdk.tech_lef),
        ("stdcell_lef", pdk.stdcell_lef),
        ("wire_rc", pdk.layer_rc),
        ("liberty", spec.liberty),
    )
    sdc_files = tuple((f"sdc[{i}]", path) for i, path in enumerate(spec.timing.sdc))
    environment = _environment_record()
    reason = _decline_reason(pdk_files, sdc_files)
    if reason is not None or environment is None:
        reason = reason or "a preload library is set, or PATH has a relative entry"
        logger.info("OpenROAD reuse declined: %s", reason)
        return None
    files = (
        ("netlist", plan.build_dir / f"sta_{spec.design_name}.v"),
        ("script", plan.build_dir / "run_openroad.tcl"),
        *pdk_files,
        *sdc_files,
    )
    texts = (
        ("recipe", pipeline.openroad_recipe_text(spec)),
        *identity,
        ("environment", environment),
    )
    return OpenRoadInputs(files=files, texts=texts)


def _decline_reason(
    pdk_files: tuple[tuple[str, Path], ...], sdc_files: tuple[tuple[str, Path], ...]
) -> str | None:
    """Why a file input cannot be keyed soundly, or ``None``."""
    for role, path in pdk_files + sdc_files:
        if not path.is_absolute():
            # OpenROAD resolves it in the build dir, Booley in its own cwd.
            return f"{role} path {path} is relative"
    wire_rc = next(path for role, path in pdk_files if role == "wire_rc")
    liberty = next(path for role, path in pdk_files if role == "liberty")
    return _tcl_files_decline_reason((("wire_rc", wire_rc), *sdc_files)) or (
        _liberty_decline_reason(liberty)
    )


def sandbox_identity() -> tuple[tuple[str, str], ...] | None:
    """Records 6-9: OpenROAD build, Sandbox image, Booley build, CPU; ``None`` declines."""
    image = _image_identity()
    openroad = _openroad_identity() if image is not None else None
    if image is None or openroad is None:
        return None
    build = current_build_metadata()
    return (
        ("openroad_identity", openroad),
        ("image_identity", image),
        ("booley_version", f"{build.version}\n{build.revision}\n{build.payload_fingerprint}"),
        ("cpu_identity", _cpu_identity()),
    )


def tcl_decline_reason(text: bytes) -> str | None:
    """Why an SDC/wire-RC file is not provably side-effect free, or ``None``.

    Every line must be blank, a full-line ``#`` comment, or exactly one
    allowlisted constraint command whose arguments are plain words, one-line
    brace-lists, or getter brackets. With no ``$ \\ " ;``, Tcl performs no
    variable or backslash substitution and runs one command per line; the only
    command substitutions are the getters, whose arguments are literal. The
    character rules make the grammar's lines exactly Tcl's lines.
    """
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    bad = next((c for c in decoded if unicodedata.category(c) == "Cc" and c not in "\t\n\r"), None)
    if bad is not None:
        return f"control character {bad!r}"
    if "\r" in decoded.replace("\r\n", ""):
        return "carriage return without newline"
    for number, raw in enumerate(decoded.replace("\r\n", "\n").split("\n"), 1):
        reason = _line_decline_reason(raw)
        if reason is not None:
            return f"line {number}: {reason}"
    return None


def _line_decline_reason(raw: str) -> str | None:
    """Classify one line after stripping only space and tab (Tcl's separators)."""
    line = raw.strip(" \t")
    if not line or line.startswith("#"):
        return None
    if any(c in ';\\$"' or (c != "\t" and not " " <= c <= "~") for c in raw):
        return "non-ASCII character or one of ';', '\\', '$', '\"' in a command"
    match = _COMMAND_LINE.fullmatch(raw)
    if match is None:
        return "argument is not a plain word, brace-list or getter"
    if match["cmd"] not in _SDC_COMMANDS:
        return f"command {match['cmd']!r} is not allowed"
    bracket = next((g for g in _BRACKET_COMMAND.findall(raw) if g not in _SDC_GETTERS), None)
    if bracket is not None:
        return f"bracket command {bracket!r} is not allowed"
    return None


def _tcl_files_decline_reason(files: tuple[tuple[str, Path], ...]) -> str | None:
    for role, path in files:
        try:
            reason = tcl_decline_reason(path.read_bytes())
        except OSError as exc:
            return f"{role} {path} is unreadable: {exc}"
        if reason is not None:
            return f"{role} {path} {reason}"
    return None


def _liberty_decline_reason(liberty: Path) -> str | None:
    try:
        content = liberty.read_bytes()
    except OSError as exc:
        return f"liberty {liberty} is unreadable: {exc}"
    # A gzip liberty (read transparently) would hide include_file from this check.
    if content.startswith(b"\x1f\x8b") or b"include_file" in content:
        return f"liberty {liberty} is compressed or uses include_file"
    return None


def _environment_record() -> str | None:
    """Sorted ``NAME=value`` lines of the keyed environment; ``None`` declines.

    Preloaded libraries name code the key would not hash.
    """
    preload = Path("/etc/ld.so.preload")
    if os.environ.get("LD_PRELOAD") or os.environ.get("LD_AUDIT") or preload.exists():
        return None
    # A relative (or empty) PATH entry resolves from Booley's cwd, not the build dir.
    if any(
        not Path(entry).is_absolute() for entry in os.environ.get("PATH", "").split(os.pathsep)
    ):
        return None
    names = sorted(
        name for name in os.environ if name in _ENV_EXACT or name.startswith(_ENV_PREFIXES)
    )
    return "\n".join(f"{name}={os.environ[name]}" for name in names)


def _openroad_identity() -> str | None:
    """Resolved binary, version and content digests of it and its libraries."""
    found = shutil.which("openroad")
    if found is None:
        logger.info("OpenROAD reuse declined: openroad is not on PATH")
        return None
    binary = Path(found).resolve()
    try:
        if binary.read_bytes()[:4] != b"\x7fELF":
            logger.info("OpenROAD reuse declined: %s is not an ELF binary", binary)
            return None
        version = _run([found, "-version"])
        libraries = _ldd_paths(_run(["ldd", str(binary)]))
        lines = [str(binary), version, _sha256(binary)]
        lines += [f"{path} {_sha256(path)}" for path in libraries]
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("OpenROAD reuse declined: cannot identify OpenROAD: %s", exc)
        return None
    return "\n".join(lines)


def _run(argv: list[str]) -> str:
    return subprocess.run(
        argv, capture_output=True, text=True, check=True, timeout=_SUBPROCESS_TIMEOUT_S
    ).stdout


def _ldd_paths(output: str) -> list[Path]:
    """Resolved library paths from ``ldd`` output; raises on an unresolved library."""
    paths: list[Path] = []
    for line in output.splitlines():
        mapped = "=>" in line
        target = line.split("=>", 1)[-1].strip()
        if mapped and not target.startswith("/"):
            # "not found", or a relative LD_LIBRARY_PATH entry ldd resolved from its cwd.
            raise OSError(f"library not resolved to an absolute path: {line.strip()}")
        if target.startswith("/"):  # the vdso has no file
            paths.append(Path(target.split(" (", 1)[0]).resolve())
    return paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _image_identity() -> str | None:
    """Sandbox image build values plus the package inventory; ``None`` outside a Sandbox."""
    built_at = os.environ.get("BOOLEY_IMAGE_BUILT_AT", "")
    if os.environ.get("BOOLEY_CONTAINER") != "1" or built_at in {"", "unknown"}:
        logger.info("OpenROAD reuse declined: no identifiable Sandbox image")
        return None
    status = Path("/var/lib/dpkg/status")
    packages = _sha256(status) if status.is_file() else "absent"
    values = [f"{name}={os.environ.get(name, '')}" for name in _IMAGE_ENV]
    return "\n".join([*values, f"dpkg={packages}"])


def _cpu_identity() -> str:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "absent"
    first = text.split("\n\n", 1)[0]
    keep = ("vendor_id", "model name", "flags")
    return "\n".join(line for line in first.splitlines() if line.split(":", 1)[0].strip() in keep)


class ReuseSlot:
    """One Target's stored OpenROAD outputs in one checkout.

    Callers hold the Target's work-root lease. ``slot.json`` is published
    atomically after its generation directory is complete, so a crash leaves
    the previous manifest or none, never one naming half-written files.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def lookup(self, key: str) -> SlotEntry | None:
        """The published entry when it was stored under *key*, else ``None``."""
        try:
            manifest = json.loads((self.root / _MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(manifest, dict) or manifest.get("schema") != _SLOT_SCHEMA:
            return None
        if manifest.get("key") != key:
            return None
        return SlotEntry(
            producer=Producer(**manifest["producer"]),
            generation=self.root / manifest["generation"],
            files=tuple(manifest["files"]),
        )

    def restore(self, entry: SlotEntry, build_dir: Path) -> bool:
        """Copy *entry*'s outputs into *build_dir* as new files; ``False`` leaves none."""
        copied: list[Path] = []
        try:
            for name in entry.files:
                destination = build_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                # copyfile, not copy2: new mtimes keep the stale-artifact guard unchanged.
                shutil.copyfile(entry.generation / name, destination)
                copied.append(destination)
        except OSError as exc:
            logger.info("OpenROAD reuse slot %s unusable: %s", self.root, exc)
            for path in copied:
                path.unlink(missing_ok=True)
            return False
        return True

    def store(self, key: str, build_dir: Path, design: str, producer: Producer) -> None:
        """Publish this run's OpenROAD outputs under *key*, replacing the slot."""
        generation = f"gen-{uuid.uuid4().hex}"
        reports = sorted(p for p in (build_dir / "reports").rglob("*") if p.is_file())
        names = [
            "openroad.log",
            f"openroad_{design}.v",
            *(p.relative_to(build_dir).as_posix() for p in reports),
        ]
        for name in names:
            destination = self.root / generation / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(build_dir / name, destination)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
        atomic_write_json(
            self.root / _MANIFEST,
            {
                "schema": _SLOT_SCHEMA,
                "key": key,
                "producer": {"run_id": producer.run_id, "started_at": producer.started_at},
                "generation": generation,
                "files": names,
            },
        )
        self._remove_generations(keep=generation)

    def invalidate(self) -> None:
        """Remove the slot so an unfinished OpenROAD run can never leave one that hits."""
        (self.root / _MANIFEST).unlink(missing_ok=True)
        self._remove_generations(keep=None)

    def _remove_generations(self, keep: str | None) -> None:
        if not self.root.is_dir():
            return
        for child in self.root.glob("gen-*"):
            if child.name != keep:
                shutil.rmtree(child, ignore_errors=True)
