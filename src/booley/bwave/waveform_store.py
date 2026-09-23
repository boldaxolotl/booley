"""B-Wave-owned waveform-store inspection, conversion, and discovery.

This module is the reusable boundary between waveform mechanics and callers'
policy.  It knows FST and the native B-Wave process; it does not know
Simulation attempts, manifests, freshness, or Trace Artifact publication.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO

from booley.bwave.contract import BWaveListMetadata, decode_list_metadata
from booley.core.boundary import BoundaryError

_FST_HEADER_BLOCK = 0
_FST_HEADER_LENGTH = 329
_BWAVE_MIN_SIZE = 16
_FST_VCDATA_BLOCKS = frozenset({1, 5, 8})
_FST_MAX_BLOCK_SCAN = 4096
_DIAGNOSTIC_LIMIT = 4000
_BWAVE_EXIT_TIMEOUT_SECONDS = 15
_BWAVE_KILL_TIMEOUT_SECONDS = 5


class StoreStructure(Enum):
    """Structural classification of an FST candidate."""

    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    TOO_SMALL = "too_small"
    MALFORMED = "malformed"
    NO_VALUE_CHANGES = "no_value_changes"
    VIABLE = "viable"


@dataclass(frozen=True)
class StoreMetadata:
    """Reader-decoded metadata for a queryable store."""

    display_scope: str
    root_scopes: tuple[str, ...]
    signal_count: int
    total_ticks: int


@dataclass(frozen=True)
class StoreInspection:
    """Separate structural viability from reader-proven queryability."""

    path: Path
    structure: StoreStructure
    metadata: StoreMetadata | None = None
    failure_kind: str = ""
    detail: str = ""

    @property
    def structurally_usable(self) -> bool:
        return self.structure is StoreStructure.VIABLE

    @property
    def queryable(self) -> bool:
        return self.structurally_usable and self.metadata is not None and not self.failure_kind


@dataclass(frozen=True)
class ConversionAttempt:
    """One native ``bwave build`` invocation."""

    scope: str | None
    return_code: int
    stderr: str = ""


class ExistingStorePolicy(Enum):
    """Caller-owned policy for a pre-existing conversion destination."""

    REFUSE = "refuse"
    REUSE_STRUCTURALLY_VALID = "reuse_structurally_valid"
    REPLACE = "replace"


@dataclass(frozen=True)
class ConversionResult:
    """Structured outcome of converting VCD into an FST store."""

    destination: Path
    attempts: tuple[ConversionAttempt, ...] = ()
    inspection: StoreInspection | None = None
    failure_kind: str = ""
    detail: str = ""
    events: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return (
            not self.failure_kind
            and self.inspection is not None
            and self.inspection.structurally_usable
        )


@dataclass(frozen=True)
class DiscoveryResult:
    """Waveform selected from explicit cache and publication locations."""

    selected: Path | None
    skipped_invalid: tuple[StoreInspection, ...] = ()
    conversion: ConversionResult | None = None
    failure_kind: str = ""
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.selected is not None and not self.failure_kind


def native_bwave_binary() -> str | None:
    """Locate the native B-Wave binary without invoking the Python wrapper."""
    from booley.runtime.paths import native_bwave_binary as resolve_binary

    found = resolve_binary()
    return str(found) if found else None


def can_stream_waveform() -> bool:
    """Return whether this host can use native FIFO conversion."""
    return os.name == "posix" and native_bwave_binary() is not None


def inspect_store(
    path: Path,
    *,
    expected_scope: str | None = None,
    probe_reader: bool = True,
) -> StoreInspection:
    """Inspect one store without conflating structure and native readability."""
    structure = _inspect_structure(path)
    if structure is not StoreStructure.VIABLE:
        return StoreInspection(
            path, structure, failure_kind="structure", detail=_structure_detail(structure)
        )
    if not probe_reader:
        return StoreInspection(path, structure)
    metadata, failure_kind, detail = _read_metadata(path)
    if metadata is None:
        return StoreInspection(path, structure, failure_kind=failure_kind, detail=detail)
    if metadata.signal_count <= 0:
        return StoreInspection(
            path,
            structure,
            _store_metadata(metadata),
            failure_kind="zero_signals",
            detail="retained trace has no signals",
        )
    if expected_scope and not metadata.contains_scope(expected_scope):
        return StoreInspection(
            path,
            structure,
            _store_metadata(metadata),
            failure_kind="scope_mismatch",
            detail=(
                f"retained trace scope {metadata.display_scope!r} does not contain "
                f"expected DUT scope {expected_scope!r}"
            ),
        )
    return StoreInspection(path, structure, _store_metadata(metadata))


def convert_vcd(
    vcd_path: Path,
    store_path: Path,
    *,
    scope: str | None = None,
    allow_unscoped_fallback: bool = False,
    existing_store: ExistingStorePolicy = ExistingStorePolicy.REPLACE,
) -> ConversionResult:
    """Convert VCD to FST and return ordered attempts and bounded diagnostics."""
    if not vcd_path.is_file():
        return ConversionResult(
            store_path, failure_kind="missing_input", detail=f"VCD not found: {vcd_path}"
        )
    existing = inspect_store(store_path, probe_reader=False)
    if result := _apply_existing_store_policy(store_path, existing, existing_store):
        return result
    binary = native_bwave_binary()
    if not binary:
        return ConversionResult(
            store_path,
            failure_kind="missing_binary",
            detail="native B-Wave binary is unavailable",
        )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    scopes = [scope]
    if scope and allow_unscoped_fallback:
        scopes.append(None)
    attempts: list[ConversionAttempt] = []
    events = ["post-processing VCD -> .fst"]
    for attempt_scope in scopes:
        _prepare_conversion_destination(store_path, existing_store)
        if attempt_scope is None and scope is not None:
            events.append("scoped post-process produced no queryable store; retrying whole trace")
        completed = _run_build(binary, vcd_path, store_path, attempt_scope)
        stderr = _bounded(_decode_output(completed.stderr))
        attempts.append(ConversionAttempt(attempt_scope, completed.returncode, stderr))
        inspection = inspect_store(store_path, probe_reader=False)
        if completed.returncode == 0 and inspection.structurally_usable:
            size_mb = store_path.stat().st_size / (1024 * 1024)
            events.append(f"wrote {store_path.name} ({size_mb:.1f} MB)")
            return ConversionResult(store_path, tuple(attempts), inspection, events=tuple(events))
    inspection = inspect_store(store_path, probe_reader=False)
    detail = _conversion_detail(vcd_path, store_path, scope, attempts)
    return ConversionResult(
        store_path,
        tuple(attempts),
        inspection,
        failure_kind="conversion_failed",
        detail=detail,
        events=tuple(events),
    )


def _apply_existing_store_policy(
    store_path: Path,
    inspection: StoreInspection,
    policy: ExistingStorePolicy,
) -> ConversionResult | None:
    if store_path.exists() and policy is ExistingStorePolicy.REFUSE:
        return ConversionResult(
            store_path,
            inspection=inspection,
            failure_kind="existing_store",
            detail=f"refusing to overwrite existing store: {store_path}",
        )
    if inspection.structurally_usable and (policy is ExistingStorePolicy.REUSE_STRUCTURALLY_VALID):
        return ConversionResult(
            store_path,
            inspection=inspection,
            events=("reused existing store",),
        )
    return None


def _prepare_conversion_destination(store_path: Path, policy: ExistingStorePolicy) -> None:
    current = inspect_store(store_path, probe_reader=False)
    if store_path.exists() and (
        policy is ExistingStorePolicy.REPLACE or not current.structurally_usable
    ):
        store_path.unlink(missing_ok=True)


def waveform_cache_dir(work_dir: Path, cache_key: str | None = None) -> Path:
    """Return the collision-resistant cache bucket for *work_dir*."""
    if cache_key:
        name = cache_key
    else:
        resolved = str(work_dir.resolve())
        digest = hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:12]
        name = f"{work_dir.name}-{digest}"
    path = Path(tempfile.gettempdir()) / "bwave" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def discover_waveform(
    work_dir: Path,
    *,
    cache_dir: Path,
    convert_raw_vcd: bool = True,
    materialize_converted_store: bool = True,
) -> DiscoveryResult:
    """Discover a store or raw VCD without writing Simulation-owned state."""
    cached, cached_invalid, cached_error = _stores_in(cache_dir)
    published, published_invalid, published_error = _stores_in(work_dir)
    skipped = (*cached_invalid, *published_invalid)
    if cached_error or published_error:
        return DiscoveryResult(
            None, skipped, failure_kind="ambiguous", detail=cached_error or published_error
        )
    selected = _select_store(cached, published)
    if selected is not None:
        return DiscoveryResult(selected, skipped)
    vcd = work_dir / "trace.vcd"
    if not vcd.exists():
        vcd = _materialize_fifo_vcd(work_dir / "trace.fifo", vcd) or vcd
    if not vcd.exists():
        return DiscoveryResult(None, skipped)
    if not convert_raw_vcd:
        return DiscoveryResult(vcd, skipped)
    destination = cache_dir / "trace.fst"
    conversion = convert_vcd(
        vcd,
        destination,
        existing_store=ExistingStorePolicy.REUSE_STRUCTURALLY_VALID,
    )
    if not conversion.success:
        return DiscoveryResult(vcd, skipped, conversion=conversion)
    selected = destination
    if materialize_converted_store:
        published_path = work_dir / "trace.fst"
        try:
            if destination.resolve() != published_path.resolve():
                shutil.copy2(destination, published_path)
            selected = published_path
        except OSError:
            selected = destination
    return DiscoveryResult(selected, skipped, conversion=conversion)


class StreamingConversion:
    """Opaque owner of one native FIFO conversion process."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        keepalive_fd: int,
        fifo_path: Path,
        store_path: Path,
        stderr_path: Path,
        scope: str | None,
    ) -> None:
        self._process = process
        self._keepalive_fd: int | None = keepalive_fd
        self.fifo_path = fifo_path
        self.store_path = store_path
        self.stderr_path = stderr_path
        self._scope = scope

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def return_code(self) -> int | None:
        return self._process.poll()

    def poll(self) -> int | None:
        return self._process.poll()

    def progress_size(self) -> int:
        total = 0
        for path in (
            self.store_path,
            self.store_path.with_name(self.store_path.name + ".progress"),
        ):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
        return total

    def stderr_tail(self, limit: int = 4000) -> str:
        try:
            return self.stderr_path.read_text(errors="replace")[-limit:].strip()
        except OSError:
            return ""

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            self._process.kill()

    def finish(self) -> ConversionResult:
        """Close the keepalive, wait with kill escalation, and remove FIFO state."""
        if self._keepalive_fd is not None:
            os.close(self._keepalive_fd)
            self._keepalive_fd = None
        events: list[str] = []
        try:
            return_code = self._process.wait(timeout=_BWAVE_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.kill()
            return_code = self._process.wait(timeout=_BWAVE_KILL_TIMEOUT_SECONDS)
            events.append("WARNING: bwave killed (FIFO hung)")
        stderr = self.stderr_tail()
        inspection = inspect_store(self.store_path, probe_reader=False)
        if return_code == 0 and inspection.structurally_usable:
            size_mb = self.store_path.stat().st_size / (1024 * 1024)
            events.append(f"wrote {self.store_path.name} ({size_mb:.1f} MB)")
        elif return_code != 0:
            events.append(f"WARNING: bwave exited with rc={return_code}")
        self.fifo_path.unlink(missing_ok=True)
        self.store_path.with_name(self.store_path.name + ".progress").unlink(missing_ok=True)
        attempt = ConversionAttempt(self._scope, return_code, _bounded(stderr))
        return ConversionResult(
            self.store_path,
            (attempt,),
            inspection,
            failure_kind="" if inspection.structurally_usable else "conversion_failed",
            detail=stderr,
            events=tuple(events),
        )


def start_streaming_conversion(
    fifo_path: Path,
    store_path: Path,
    *,
    scope: str | None = None,
    stderr_path: Path,
) -> StreamingConversion | None:
    """Start the B-Wave half of FIFO conversion, or return unavailable."""
    if os.name != "posix":
        return None
    binary = native_bwave_binary()
    if not binary:
        return None
    import fcntl

    fifo_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    fifo_path.unlink(missing_ok=True)
    os.mkfifo(str(fifo_path))
    keepalive_fd = os.open(str(fifo_path), os.O_RDWR)
    read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
    fcntl.fcntl(read_fd, fcntl.F_SETFL, fcntl.fcntl(read_fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    command = [binary, "build", "-o", str(store_path)]
    if scope:
        command.extend(["--scope", scope])
    stderr_file = stderr_path.open("w")
    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=read_fd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
        except OSError:
            os.close(keepalive_fd)
            fifo_path.unlink(missing_ok=True)
            return None
    finally:
        stderr_file.close()
        os.close(read_fd)
    return StreamingConversion(process, keepalive_fd, fifo_path, store_path, stderr_path, scope)


def _inspect_structure(path: Path) -> StoreStructure:
    try:
        status = path.stat()
        if not stat.S_ISREG(status.st_mode):
            return StoreStructure.NOT_REGULAR
        if status.st_size < _BWAVE_MIN_SIZE:
            return StoreStructure.TOO_SMALL
        with path.open("rb") as stream:
            header = stream.read(9)
            if (
                len(header) != 9
                or header[0] != _FST_HEADER_BLOCK
                or int.from_bytes(header[1:9], "big") != _FST_HEADER_LENGTH
            ):
                return StoreStructure.MALFORMED
            return _scan_blocks(stream, status.st_size)
    except FileNotFoundError:
        return StoreStructure.MISSING
    except OSError:
        return StoreStructure.NOT_REGULAR


def _scan_blocks(stream: IO[bytes], size: int) -> StoreStructure:
    offset = 1 + _FST_HEADER_LENGTH
    for _ in range(_FST_MAX_BLOCK_SCAN):
        if offset >= size:
            return StoreStructure.NO_VALUE_CHANGES
        stream.seek(offset)
        block = stream.read(9)
        if len(block) != 9:
            return StoreStructure.MALFORMED
        if block[0] in _FST_VCDATA_BLOCKS:
            return StoreStructure.VIABLE
        length = int.from_bytes(block[1:9], "big")
        if length <= 0 or offset + 1 + length > size:
            return StoreStructure.MALFORMED
        offset += 1 + length
    return StoreStructure.MALFORMED


def _read_metadata(path: Path) -> tuple[BWaveListMetadata | None, str, str]:
    binary = native_bwave_binary()
    if not binary:
        return None, "missing_binary", "native B-Wave binary is unavailable for trace validation"
    try:
        result = subprocess.run(
            [binary, "list", str(path), "--format", "json", "--limit", "1"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return None, "reader_timeout", f"B-Wave hierarchy probe failed: {exc}"
    except OSError as exc:
        return None, "reader_error", f"B-Wave hierarchy probe failed: {exc}"
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {lines[-1]}" if lines else ""
        return (
            None,
            "reader_error",
            f"B-Wave could not read the retained trace (rc={result.returncode}){suffix}",
        )
    try:
        return decode_list_metadata(result.stdout), "", ""
    except (BoundaryError, json.JSONDecodeError) as exc:
        return None, "malformed_metadata", f"B-Wave returned malformed trace metadata: {exc}"


def _store_metadata(metadata: BWaveListMetadata) -> StoreMetadata:
    return StoreMetadata(
        metadata.display_scope,
        tuple(metadata.root_scopes),
        metadata.signal_count,
        metadata.total_ticks,
    )


def _stores_in(
    directory: Path,
) -> tuple[list[Path], tuple[StoreInspection, ...], str]:
    if not directory.is_dir():
        return [], (), ""
    valid: list[Path] = []
    invalid: list[StoreInspection] = []
    try:
        candidates = sorted(directory.glob("*.fst"))
    except OSError:
        return [], (), ""
    for candidate in candidates:
        inspection = inspect_store(candidate, probe_reader=False)
        if inspection.structurally_usable:
            valid.append(candidate)
        else:
            invalid.append(inspection)
    if len(valid) > 1:
        detail = f"ERROR: Multiple *.fst files in {directory}:\n" + "\n".join(
            f"  {path.name}" for path in valid
        )
        return valid, tuple(invalid), detail
    return valid, tuple(invalid), ""


def _select_store(cached: list[Path], published: list[Path]) -> Path | None:
    if cached and published:
        return (
            published[0]
            if published[0].stat().st_mtime >= cached[0].stat().st_mtime
            else cached[0]
        )
    return (cached or published or [None])[0]


def _looks_like_vcd(path: Path) -> bool:
    try:
        status = path.stat()
        if stat.S_ISFIFO(status.st_mode) or status.st_size == 0:
            return False
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError:
        return False
    return b"$date" in head and (b"$scope" in head or b"$timescale" in head)


def _materialize_fifo_vcd(fifo_path: Path, vcd_path: Path) -> Path | None:
    if not _looks_like_vcd(fifo_path):
        return None
    try:
        if vcd_path.exists() and vcd_path.stat().st_mtime >= fifo_path.stat().st_mtime:
            return vcd_path
        shutil.copy2(fifo_path, vcd_path)
    except OSError:
        return fifo_path
    return vcd_path


def _run_build(
    binary: str,
    vcd_path: Path,
    store_path: Path,
    scope: str | None,
) -> subprocess.CompletedProcess[bytes]:
    command = [binary, "build", "-o", str(store_path)]
    if scope:
        command.extend(["--scope", scope])
    with vcd_path.open("rb") as stream:
        return subprocess.run(command, stdin=stream, capture_output=True, check=False)


def _decode_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip()
    return (value or "").strip()


def _bounded(value: str) -> str:
    return value[-_DIAGNOSTIC_LIMIT:]


def _conversion_detail(
    vcd_path: Path,
    store_path: Path,
    scope: str | None,
    attempts: list[ConversionAttempt],
) -> str:
    lines = [
        f"vcd_path: {vcd_path}",
        f"vcd_exists: {vcd_path.exists()}",
        f"requested_scope: {scope or '<all>'}",
        f"bwave_path: {store_path}",
        f"bwave_exists: {store_path.exists()}",
    ]
    for label, attempt in enumerate(attempts, start=1):
        if attempt.stderr:
            lines.extend(
                [
                    "",
                    f"[attempt {label}; scope={attempt.scope or '<all>'}; rc={attempt.return_code}]",
                    attempt.stderr,
                ]
            )
    return "\n".join(lines)


def _structure_detail(structure: StoreStructure) -> str:
    return {
        StoreStructure.MISSING: "store does not exist",
        StoreStructure.NOT_REGULAR: "store is not a regular file",
        StoreStructure.TOO_SMALL: "store is too small to contain an FST trace",
        StoreStructure.MALFORMED: "FST header or block chain is malformed",
        StoreStructure.NO_VALUE_CHANGES: "FST contains no value-change data",
        StoreStructure.VIABLE: "",
    }[structure]
