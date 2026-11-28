"""Crash-safe storage for one durable Simulation Record.

The store deliberately knows nothing about process execution or Criteria.  It
owns the small append-only protocol which makes resume deterministic: a
manifest is published first, attempts are append-only, and ``result.json`` is
the sole completion marker for a work item.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from booley.core.atomic_files import atomic_replace_bytes
from booley.core.booley_dir_links import is_booley_dir_entry
from booley.flows.artifact_durability import (
    discard_published_staging,
    discard_staged_files,
    durable_create,
    durable_directory,
    fsync_directory,
)
from booley.flows.sim.record_files import read_bounded, read_json_object
from booley.flows.sim.record_layout import RECORD_DIRECTORY, refuse_older_manifest_path
from booley.flows.sim.record_reports import is_report_link
from booley.runtime.file_lock import nonblocking_file_lock

from .codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SimulationRecordIntegrityError,
    UnsupportedSimulationRecordFormatError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_simulation_attempt,
    decode_simulation_record_manifest,
    decode_simulation_result,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_simulation_attempt,
    encode_simulation_record_manifest,
    encode_simulation_result,
    reads_simulation_record_documents,
    refuse_retired_document,
    validate_relative_path,
)
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    SimulationAttempt,
    SimulationRecordDocument,
    SimulationRecordManifest,
    SimulationResult,
    SimulatorBundle,
    StrictGrade,
)
from .planning import manifest_digest
from .pre_sim_evidence import PreSimFiring, _reference, decode_pre_sim_firing

_T = TypeVar("_T", bound=SimulationRecordDocument)
_SUMMARY_SCHEMA = "booley.simulation-record-summary/v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class WorkItemRecovery:
    """Validated durable state for one manifest work item."""

    work_item_id: str
    state: str
    attempt_count: int
    result: SimulationResult | None


@dataclass(frozen=True, slots=True)
class SimulationRecordRecovery:
    """Manifest-ordered recovery classification."""

    items: tuple[WorkItemRecovery, ...]

    @property
    def complete(self) -> tuple[str, ...]:
        return tuple(item.work_item_id for item in self.items if item.state == "complete")

    @property
    def interrupted(self) -> tuple[str, ...]:
        return tuple(item.work_item_id for item in self.items if item.state == "interrupted")

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(item.work_item_id for item in self.items if item.state == "pending")


@dataclass(frozen=True, slots=True)
class SharedBuildRecovery:
    """One settled shared build of an invocation, as its records state it."""

    directory: Path
    attempt: BundleBuildAttempt
    result: BundleBuildResult
    bundle: SimulatorBundle | None
    build_execution: Mapping[str, object] | None


def _invocation_of(simulation_record_root: Path) -> Path | None:
    """The invocation directory holding ``<invocation>/targets/<target>/record``, if laid out so."""
    parents = simulation_record_root.parents
    if len(parents) > 2 and parents[1].name == "targets":
        return parents[2]
    return None


def _require_safe_parents(path: Path, root: Path, booley_dir: Path | None = None) -> None:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationRecordIntegrityError(f"record path escapes store: {path}") from exc
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if is_report_link(current) and not is_booley_dir_entry(current, booley_dir):
            raise SimulationRecordIntegrityError(f"record path contains a link: {current}")


def _create_immutable(path: Path, raw: bytes) -> None:
    durable_create(path, raw)


class SimulationRecordStore:
    """Append-only authoritative storage rooted at one ``record`` directory."""

    def __init__(
        self, simulation_record_directory: Path, *, booley_dir: Path | None = None
    ) -> None:
        self.root = simulation_record_directory.absolute()
        # The one link component the store may cross (#2191); ``None`` is strict.
        self.booley_dir = booley_dir

    def _read(self, path: Path, *, limit: int) -> bytes:
        discard_published_staging(path)
        return read_bounded(
            path, limit=limit, error=SimulationRecordIntegrityError, booley_dir=self.booley_dir
        )

    def _decode_located(self, decode: Callable[[bytes], _T], path: Path, limit: int) -> _T:
        """Read and decode one record; a Booley 0.3.0 refusal names its invocation."""
        raw = self._read(path, limit=limit)
        try:
            return decode(raw)
        except UnsupportedSimulationRecordFormatError as exc:
            raise self._locate(exc) from None

    def _locate(
        self, refusal: UnsupportedSimulationRecordFormatError
    ) -> UnsupportedSimulationRecordFormatError:
        """The refusal naming this Record's invocation directory, when it has one."""
        invocation = _invocation_of(self.root)
        return refusal if invocation is None else refusal.located(invocation)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.json"

    def publish_manifest(self, manifest: SimulationRecordManifest) -> str:
        raw = encode_simulation_record_manifest(manifest)
        _require_safe_parents(self.manifest_path, self.root, self.booley_dir)
        _create_immutable(self.manifest_path, raw)
        return manifest_digest(manifest)

    def load_manifest(self) -> SimulationRecordManifest:
        _require_safe_parents(self.manifest_path, self.root, self.booley_dir)
        return self._decode_located(
            decode_simulation_record_manifest, self.manifest_path, MANIFEST_MAX_BYTES
        )

    def manifest_sha256(self) -> str:
        return manifest_digest(self.load_manifest())

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Fail immediately when another scheduler owns this record.

        Holding it, discard the staged files a killed publication left in the
        record and its Target directory: only the lock holder writes there.
        """
        _require_safe_parents(self.root, self.root, self.booley_dir)
        if not self.root.is_dir():
            raise SimulationRecordIntegrityError("Simulation Record directory disappeared")
        path = self.root / ".lock"
        _require_safe_parents(path, self.root, self.booley_dir)
        with path.open("a+", encoding="utf-8") as stream, nonblocking_file_lock(stream):
            discard_staged_files(self.root.parent)
            for staged in self.root.glob(".summary.json.*.tmp"):
                staged.unlink(missing_ok=True)
            yield

    def work_item_directory(self, work_item_id: str) -> Path:
        manifest = self.load_manifest()
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        for item in items:
            if item["work_item_id"] == work_item_id:
                return self._work_item_directory(item)
        raise SimulationRecordIntegrityError(f"unknown work item {work_item_id!r}")

    def _work_item_directory(self, item: Mapping[str, object]) -> Path:
        work_item_id = cast(str, item["work_item_id"])
        ordinal = cast(int, item["ordinal"]) + 1
        digest = work_item_id.rsplit(":", 1)[-1]
        name = f"{ordinal:04d}-{digest}"
        # Decode does not check the id, and a path glob assumes one component:
        # refuse a "/" here, and a backslash (a separator on Windows) below.
        if "/" in name:
            raise SimulationRecordIntegrityError(
                f"work-item directory name {name!r} is not one contained path component"
            )
        validate_relative_path(name)
        return self.root / "work-items" / name

    def allocate_attempt_directory(self, work_item_id: str, attempt_id: str) -> tuple[int, Path]:
        attempts = self.work_item_directory(work_item_id) / "attempts"
        durable_directory(attempts)
        existing = self._attempt_directories(attempts)
        ordinal = len(existing) + 1
        if ordinal > 10_000:
            raise SimulationRecordIntegrityError("work-item attempt ceiling exceeded")
        path = attempts / f"{ordinal:04d}-{attempt_id}"
        path.mkdir(mode=0o700)
        fsync_directory(attempts)
        return ordinal, path

    def allocate_build_attempt_directory(
        self, build_variant_id: str, build_attempt_id: str
    ) -> tuple[int, Path]:
        """Allocate one append-only record-owned shared build attempt."""
        digest = _variant_digest(build_variant_id)
        attempts = self.root / "build-variants" / digest / "attempts"
        durable_directory(attempts)
        existing = self._attempt_directories(attempts)
        ordinal = len(existing) + 1
        if ordinal > 10_000:
            raise SimulationRecordIntegrityError("shared build attempt ceiling exceeded")
        path = attempts / f"{ordinal:04d}-{build_attempt_id}"
        path.mkdir(mode=0o700)
        fsync_directory(attempts)
        return ordinal, path

    def publish_attempt(self, directory: Path, attempt: SimulationAttempt) -> Path:
        return self._publish(directory / "attempt.json", attempt, encode_simulation_attempt)

    def publish_build_attempt(self, directory: Path, attempt: BundleBuildAttempt) -> Path:
        return self._publish(
            directory / "build-attempt.json", attempt, encode_bundle_build_attempt
        )

    def publish_build_result(self, directory: Path, result: BundleBuildResult) -> Path:
        return self._publish(directory / "build-result.json", result, encode_bundle_build_result)

    @reads_simulation_record_documents
    def recover_shared_build(
        self, build_variant_id: str, producer_invocation_id: int
    ) -> SharedBuildRecovery | None:
        """Return the one settled build of this invocation, if its records state one."""
        digest = _variant_digest(build_variant_id)
        attempts = self._attempt_directories(self.root / "build-variants" / digest / "attempts")
        recovered: list[SharedBuildRecovery] = []
        for directory in attempts:
            candidate = self._recover_shared_attempt(directory, producer_invocation_id)
            if candidate is not None:
                recovered.append(candidate)
        if len(recovered) > 1:
            raise SimulationRecordIntegrityError(
                "multiple settled shared builds exist for one variant/invocation"
            )
        return recovered[0] if recovered else None

    def _recover_shared_attempt(
        self, directory: Path, invocation_id: int
    ) -> SharedBuildRecovery | None:
        attempt_path = directory / "build-attempt.json"
        if not attempt_path.exists():
            return None
        attempt = decode_bundle_build_attempt(self._read(attempt_path, limit=RECORD_MAX_BYTES))
        result_path = directory / "build-result.json"
        if not result_path.exists():
            return None
        result = decode_bundle_build_result(self._read(result_path, limit=RECORD_MAX_BYTES))
        if attempt.document["producer_invocation_id"] != invocation_id:
            return None
        if result.document["state"] == "infrastructure_error":
            return None
        if result.document["state"] == "design_failure":
            return SharedBuildRecovery(directory, attempt, result, None, None)
        return SharedBuildRecovery(
            directory,
            attempt,
            result,
            self._load_shared_bundle(directory, result),
            _load_build_execution(directory, result.document, self.booley_dir),
        )

    def _load_shared_bundle(self, directory: Path, result: BundleBuildResult) -> SimulatorBundle:
        binding = cast(Mapping[str, str], result.document["bundle"])
        path = directory / binding["manifest_path"]
        return decode_simulator_bundle(self._read(path, limit=RECORD_MAX_BYTES))

    def publish_result(self, work_item_id: str, result: SimulationResult) -> Path:
        path = self.work_item_directory(work_item_id) / "result.json"
        return self._publish(path, result, encode_simulation_result)

    def publish_pre_sim_evidence(
        self,
        directory: Path,
        ordinal: int,
        raw: bytes,
        *,
        on_published: Callable[[], None] | None = None,
    ) -> Path:
        """Durably create a contained attempt sidecar without replacing an existing firing."""
        path = directory / "pre-sim" / f"{ordinal:04d}.json"
        _require_safe_parents(path, self.root, self.booley_dir)
        if len(raw) > MANIFEST_MAX_BYTES:
            raise SimulationRecordIntegrityError("Pre-Sim Commands evidence exceeds byte limit")
        _create_immutable(path, raw)
        if on_published is not None:
            on_published()
        return path

    def latest_attempt(self, work_item_id: str) -> SimulationAttempt | None:
        """Load the final validated attempt for an interrupted work item."""
        attempts = self._attempt_directories(self.work_item_directory(work_item_id) / "attempts")
        if not attempts or not (attempts[-1] / "attempt.json").exists():
            return None
        return decode_simulation_attempt(
            self._read(attempts[-1] / "attempt.json", limit=RECORD_MAX_BYTES)
        )

    def read_attempt(self, directory: Path) -> SimulationAttempt:
        """Read one immutable contained attempt through the storage boundary."""
        return decode_simulation_attempt(
            self._read(directory / "attempt.json", limit=RECORD_MAX_BYTES)
        )

    def pre_sim_references(self, directory: Path) -> list[dict[str, object]]:
        """Read contained immutable sidecars in their ordinal order."""
        return [
            _reference(
                path, self._read(path, limit=MANIFEST_MAX_BYTES), directory.name.split("-", 1)[1]
            )
            for path in self._pre_sim_paths(directory)
        ]

    @staticmethod
    def _pre_sim_paths(directory: Path) -> list[Path]:
        root = directory / "pre-sim"
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise SimulationRecordIntegrityError("invalid Pre-Sim Commands evidence directory")
        paths = sorted(root.glob("*.json"))
        if len(paths) > 4096:
            raise SimulationRecordIntegrityError("too many Pre-Sim Commands firings")
        return paths

    @reads_simulation_record_documents
    def _attempt_firings(self, directory: Path, terminal: bool) -> tuple[PreSimFiring, ...]:
        """Parse one attempt's sidecars."""
        if not (directory / "attempt.json").exists():
            return ()
        firings: list[PreSimFiring] = []
        for ordinal, path in enumerate(self._pre_sim_paths(directory), start=1):
            raw = self._read(path, limit=MANIFEST_MAX_BYTES)
            firing = PreSimFiring(self.manifest_path, path, decode_pre_sim_firing(raw), terminal)
            if firing.document["ordinal"] != ordinal or path.name != f"{ordinal:04d}.json":
                raise SimulationRecordIntegrityError("Pre-Sim Commands ordinal disagrees")
            firings.append(firing)
        return tuple(firings)

    def _simulation_record_firings(
        self, manifest: SimulationRecordManifest, recovery: SimulationRecordRecovery
    ) -> tuple[PreSimFiring, ...]:
        """Read every retained attempt's firings, marking those the scan found terminal."""
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        firings: list[PreSimFiring] = []
        for item, recovered in zip(items, recovery.items, strict=True):
            directories = self._attempt_directories(self._work_item_directory(item) / "attempts")
            for directory in directories:
                # The scan proved a result binds the final attempt, so only it is terminal.
                terminal = recovered.result is not None and directory == directories[-1]
                firings.extend(self._attempt_firings(directory, terminal))
        return tuple(firings)

    def _publish(self, path: Path, value: _T, encoder: Callable[[_T], bytes]) -> Path:
        _require_safe_parents(path, self.root, self.booley_dir)
        _create_immutable(path, encoder(value))
        return path

    def scan(
        self, manifest: SimulationRecordManifest | None = None, manifest_sha256: str | None = None
    ) -> SimulationRecordRecovery:
        """Recover work item state without reading Pre-Sim sidecars.

        ``manifest_sha256`` is the authenticated digest of the passed ``manifest``;
        callers that already hold it supply it instead of recomputing it.
        """
        manifest = manifest or self.load_manifest()
        manifest_sha256 = manifest_sha256 or manifest_digest(manifest)
        expected_workload = cast(Mapping[str, str], manifest.document["fingerprints"])[
            "workload_sha256"
        ]
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        recovered = tuple(
            self._scan_item(item, manifest_sha256, expected_workload) for item in items
        )
        return SimulationRecordRecovery(recovered)

    def _scan_item(
        self, work_item: Mapping[str, object], manifest_sha256: str, workload_sha256: str
    ) -> WorkItemRecovery:
        work_item_id = cast(str, work_item["work_item_id"])
        # ``scan`` already decoded the complete manifest.  Re-loading and
        # re-validating it once per item makes maximum-size records quadratic.
        directory = self._work_item_directory(work_item)
        attempts_root = directory / "attempts"
        attempts = self._attempt_directories(attempts_root)
        decoded_attempts: list[SimulationAttempt] = []
        for ordinal, attempt_directory in enumerate(attempts, start=1):
            self._refuse_previous_snapshot(attempt_directory)
            attempt_path = attempt_directory / "attempt.json"
            if not attempt_path.exists() and not is_report_link(attempt_path):
                continue
            attempt = self.read_attempt(attempt_directory)
            document = attempt.document
            if (
                document["work_item_id"] != work_item_id
                or document["attempt_ordinal"] != ordinal
                or document["manifest_sha256"] != manifest_sha256
                or document["workload_sha256"] != workload_sha256
            ):
                raise SimulationRecordIntegrityError(
                    f"attempt identity disagrees for {work_item_id} ordinal {ordinal}"
                )
            _require_attempt_directory_name(attempt_directory, attempt)
            decoded_attempts.append(attempt)
        result_path = directory / "result.json"
        if result_path.exists() or is_report_link(result_path):
            result = self._scan_result(
                result_path,
                work_item_id,
                manifest_sha256,
                workload_sha256,
                attempts,
                decoded_attempts,
            )
            return WorkItemRecovery(work_item_id, "complete", len(attempts), result)
        state = "interrupted" if attempts else "pending"
        return WorkItemRecovery(work_item_id, state, len(attempts), None)

    def _refuse_previous_snapshot(self, attempt_directory: Path) -> None:
        """Refuse an attempt that holds an older Booley's executable snapshot."""
        path = attempt_directory / "snapshot" / "snapshot.json"
        if path.exists() or is_report_link(path):
            self._decode_located(refuse_retired_document, path, MANIFEST_MAX_BYTES)

    def _scan_result(
        self,
        path: Path,
        work_item_id: str,
        manifest_sha256: str,
        workload_sha256: str,
        attempts: tuple[Path, ...],
        decoded_attempts: list[SimulationAttempt],
    ) -> SimulationResult:
        result = self._decode_located(decode_simulation_result, path, RECORD_MAX_BYTES)
        document = result.document
        if (
            document["work_item_id"] != work_item_id
            or document["manifest_sha256"] != manifest_sha256
            or document["workload_sha256"] != workload_sha256
        ):
            raise SimulationRecordIntegrityError(f"result identity disagrees for {work_item_id}")
        if not decoded_attempts or decoded_attempts[-1].document["attempt_ordinal"] != len(
            attempts
        ):
            raise SimulationRecordIntegrityError("result has no published attempt")
        final_attempt = decoded_attempts[-1].document
        if (
            document["attempt_id"] != final_attempt["attempt_id"]
            or document["attempt_ordinal"] != final_attempt["attempt_ordinal"]
        ):
            raise SimulationRecordIntegrityError(
                "result does not bind the final published attempt"
            )
        return result

    @staticmethod
    def _attempt_directories(root: Path) -> tuple[Path, ...]:
        if not root.exists():
            return ()
        if is_report_link(root) or not root.is_dir():
            raise SimulationRecordIntegrityError(f"invalid attempts directory: {root}")
        children = sorted(root.iterdir(), key=lambda item: item.name)
        for ordinal, child in enumerate(children, start=1):
            prefix = f"{ordinal:04d}-"
            if not child.name.startswith(prefix) or is_report_link(child) or not child.is_dir():
                raise SimulationRecordIntegrityError(f"invalid attempt directory: {child}")
        return tuple(children)

    def regenerate_summary(
        self, manifest: SimulationRecordManifest | None = None, manifest_sha256: str | None = None
    ) -> Mapping[str, object]:
        """Regenerate the projection; ``manifest_sha256`` is the digest of the passed manifest."""
        manifest = manifest or self.load_manifest()
        manifest_sha256 = manifest_sha256 or manifest_digest(manifest)
        recovery = self.scan(manifest, manifest_sha256)
        grades = tuple(
            cast(str, item.result.document["grade"])
            for item in recovery.items
            if item.result is not None
        )
        valid_grades = {grade.value for grade in StrictGrade}
        if not all(isinstance(grade, str) and grade in valid_grades for grade in grades):
            raise SimulationRecordIntegrityError("Simulation Result grade is invalid")
        aggregate = _aggregate_grade(
            grades, complete=not recovery.pending and not recovery.interrupted
        )
        document: dict[str, object] = {
            "$schema": _SUMMARY_SCHEMA,
            "simulation_record_id": manifest.document["simulation_record_id"],
            "manifest_sha256": manifest_sha256,
            "workload_sha256": cast(Mapping[str, str], manifest.document["fingerprints"])[
                "workload_sha256"
            ],
            "complete": not recovery.pending and not recovery.interrupted,
            "aggregate_grade": aggregate,
            "completed": list(recovery.complete),
            "interrupted": list(recovery.interrupted),
            "pending": list(recovery.pending),
        }
        raw = canonical_json_bytes(document)
        if len(raw) > MANIFEST_MAX_BYTES:
            raise SimulationRecordIntegrityError("summary exceeds size ceiling")
        _require_safe_parents(self.summary_path, self.root, self.booley_dir)
        atomic_replace_bytes(self.summary_path, raw)
        return json.loads(raw)


def collect_pre_sim_firings(
    *manifest_paths: Path, booley_dir: Path | None = None
) -> tuple[PreSimFiring, ...]:
    """Read each named record's Pre-Sim firings; the only entry that does.

    Deduplicates by full firing identity ``(campaign_id, work_item_id,
    attempt_id, ordinal)``.  Each distinct manifest path is scanned once, in the
    order given.
    """
    firings = {}
    for path in dict.fromkeys(Path(path).absolute() for path in manifest_paths):
        refuse_older_manifest_path(path)
        if path.name != "manifest.json" or path.parent.name != RECORD_DIRECTORY:
            raise SimulationRecordIntegrityError("expected exact Simulation Record manifest path")
        store = SimulationRecordStore(path.parent, booley_dir=booley_dir)
        manifest = store.load_manifest()
        recovery = store.scan(manifest, manifest_digest(manifest))
        for firing in store._simulation_record_firings(manifest, recovery):
            firings[firing.key] = firing
    return tuple(firings.values())


def _require_attempt_directory_name(directory: Path, attempt: SimulationAttempt) -> None:
    """Refuse an attempt stored under a directory its own ordinal and id do not name."""
    document = attempt.document
    if directory.name != f"{document['attempt_ordinal']:04d}-{document['attempt_id']}":
        raise SimulationRecordIntegrityError("hook attempt directory owner disagrees")


def _aggregate_grade(grades: tuple[str, ...], *, complete: bool) -> str:
    if "error" in grades:
        return "error"
    if "fail" in grades:
        return "fail"
    if not complete or "inconclusive" in grades:
        return "inconclusive"
    return "pass"


def _variant_digest(build_variant_id: str) -> str:
    prefix, separator, digest = build_variant_id.partition(":")
    if prefix != "variant" or separator != ":" or _HEX64.fullmatch(digest) is None:
        raise SimulationRecordIntegrityError("invalid shared build variant identity")
    return digest


def _load_build_execution(
    directory: Path, result: Mapping[str, object], booley_dir: Path | None = None
) -> Mapping[str, object]:
    references = cast(tuple[Mapping[str, object], ...], result["evidence"])
    (match,) = [item for item in references if item["kind"] == "simulation_build_execution"]
    path = directory / cast(str, match["path"])
    discard_published_staging(path)
    document = read_json_object(
        path, limit=RECORD_MAX_BYTES, error=SimulationRecordIntegrityError, booley_dir=booley_dir
    )
    if document.get("$schema") != "booley.simulation-build-execution/v1":
        raise SimulationRecordIntegrityError("build execution evidence schema disagrees")
    return document


__all__ = [
    "SharedBuildRecovery",
    "SimulationRecordRecovery",
    "SimulationRecordStore",
    "WorkItemRecovery",
]
