"""Crash-safe storage for one durable Simulation Campaign.

The store deliberately knows nothing about process execution or Criteria.  It
owns the small append-only protocol which makes resume deterministic: a
manifest is published first, attempts are append-only, and ``result.json`` is
the sole completion marker for a work item.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from booley.flows.sim.campaign_durability import (
    durable_create,
    durable_directory,
    fsync_directory,
)
from booley.runtime.file_lock import nonblocking_file_lock

from .codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_result,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulation_campaign_manifest,
    decode_simulation_result,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_simulation_attempt,
    encode_simulation_campaign_manifest,
    encode_simulation_result,
)
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    SimulationAttempt,
    SimulationCampaignDocument,
    SimulationCampaignManifest,
    SimulationResult,
)

_T = TypeVar("_T", bound=SimulationCampaignDocument)
_SUMMARY_SCHEMA = "booley.simulation-campaign-summary/v1"


@dataclass(frozen=True, slots=True)
class WorkItemRecovery:
    """Validated durable state for one manifest work item."""

    work_item_id: str
    state: str
    attempt_count: int
    result: SimulationResult | None


@dataclass(frozen=True, slots=True)
class CampaignRecovery:
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


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_safe_parents(path: Path, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"campaign path escapes store: {path}") from exc
    current = root.absolute()
    for component in relative.parts:
        current /= component
        if _is_link(current):
            raise SimulationCampaignIntegrityError(f"campaign path contains a link: {current}")


def _create_immutable(path: Path, raw: bytes) -> None:
    durable_create(path, raw)


def _replace_projection(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_regular(path: Path, *, limit: int) -> bytes:
    if _is_link(path):
        raise SimulationCampaignIntegrityError(f"authoritative path is a link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SimulationCampaignIntegrityError(f"cannot read authoritative file {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SimulationCampaignIntegrityError(f"authoritative path is not a file: {path}")
        if info.st_size > limit:
            raise SimulationCampaignIntegrityError(f"authoritative file exceeds size ceiling: {path}")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise SimulationCampaignIntegrityError(f"authoritative file exceeds size ceiling: {path}")
        return bytes(raw)
    finally:
        os.close(descriptor)


class CampaignStore:
    """Append-only authoritative storage rooted at one ``campaign`` directory."""

    def __init__(self, campaign_directory: Path) -> None:
        self.root = campaign_directory.absolute()

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.json"

    def publish_manifest(self, manifest: SimulationCampaignManifest) -> str:
        raw = encode_simulation_campaign_manifest(manifest)
        _require_safe_parents(self.manifest_path, self.root)
        _create_immutable(self.manifest_path, raw)
        return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()

    def load_manifest(self) -> SimulationCampaignManifest:
        _require_safe_parents(self.manifest_path, self.root)
        return decode_simulation_campaign_manifest(
            _read_regular(self.manifest_path, limit=MANIFEST_MAX_BYTES)
        )

    def manifest_sha256(self) -> str:
        raw = encode_simulation_campaign_manifest(self.load_manifest())
        return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Fail immediately when another scheduler owns this campaign."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".lock"
        _require_safe_parents(path, self.root)
        with path.open("a+", encoding="utf-8") as stream, nonblocking_file_lock(stream):
            yield

    def work_item_directory(self, work_item_id: str) -> Path:
        manifest = self.load_manifest()
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        for item in items:
            if item["work_item_id"] == work_item_id:
                ordinal = cast(int, item["ordinal"]) + 1
                digest = work_item_id.rsplit(":", 1)[-1]
                return self.root / "work-items" / f"{ordinal:04d}-{digest}"
        raise SimulationCampaignIntegrityError(f"unknown work item {work_item_id!r}")

    def allocate_attempt_directory(self, work_item_id: str, attempt_id: str) -> tuple[int, Path]:
        attempts = self.work_item_directory(work_item_id) / "attempts"
        durable_directory(attempts)
        existing = self._attempt_directories(attempts)
        ordinal = len(existing) + 1
        if ordinal > 10_000:
            raise SimulationCampaignIntegrityError("work-item attempt ceiling exceeded")
        path = attempts / f"{ordinal:04d}-{attempt_id}"
        path.mkdir(mode=0o700)
        fsync_directory(attempts)
        return ordinal, path

    def publish_attempt(self, directory: Path, attempt: SimulationAttempt) -> Path:
        return self._publish(directory / "attempt.json", attempt, encode_simulation_attempt)

    def publish_build_attempt(
        self, directory: Path, attempt: BundleBuildAttempt
    ) -> Path:
        return self._publish(
            directory / "build-attempt.json", attempt, encode_bundle_build_attempt
        )

    def publish_build_result(self, directory: Path, result: BundleBuildResult) -> Path:
        return self._publish(
            directory / "build-result.json", result, encode_bundle_build_result
        )

    def publish_result(self, work_item_id: str, result: SimulationResult) -> Path:
        path = self.work_item_directory(work_item_id) / "result.json"
        return self._publish(path, result, encode_simulation_result)

    def verify_result_evidence(
        self, attempt_directory: Path, result: SimulationResult
    ) -> None:
        """Authenticate every Phase-2 private reference before result commit."""
        document = result.document
        references: list[Mapping[str, object]] = [
            cast(Mapping[str, object], document["build_result"]),
            *cast(tuple[Mapping[str, object], ...], document["evidence"]),
        ]
        snapshot = document["executable_snapshot"]
        if isinstance(snapshot, Mapping):
            references.append(cast(Mapping[str, object], snapshot["manifest"]))
        for binding in cast(tuple[Mapping[str, object], ...], document["runtime_inputs"]):
            references.append(cast(Mapping[str, object], binding["authoritative_copy"]))
        for reference in references:
            self._authenticate_reference(attempt_directory, reference)
        build_reference, build_result = self._verify_build_chain(
            attempt_directory, document
        )
        self._verify_snapshot_chain(
            attempt_directory,
            document,
            build_reference,
            build_result,
        )

    def _verify_build_chain(
        self,
        attempt_directory: Path,
        simulation: Mapping[str, object],
    ) -> tuple[Mapping[str, object], BundleBuildResult]:
        build_reference = cast(Mapping[str, object], simulation["build_result"])
        build_path = attempt_directory / cast(str, build_reference["path"])
        build_result = decode_bundle_build_result(
            _read_regular(build_path, limit=RECORD_MAX_BYTES)
        )
        build_document = build_result.document
        for field in ("campaign_id", "manifest_sha256", "workload_sha256"):
            if build_document[field] != simulation[field]:
                raise SimulationCampaignIntegrityError(
                    f"Build Result {field} disagrees with Simulation Result"
                )
        if (
            build_document["state"] != build_reference["state"]
            or build_document["build_attempt"]["build_attempt_id"]
            != build_reference["build_attempt_id"]
        ):
            raise SimulationCampaignIntegrityError(
                "Build Result identity disagrees with Simulation Result reference"
            )
        build_root = build_path.parent
        self._authenticate_reference(
            build_root,
            cast(Mapping[str, object], build_document["build_attempt"]),
        )
        bundle_binding = build_document["bundle"]
        if isinstance(bundle_binding, Mapping):
            if bundle_binding["sharing"] != build_reference["sharing"]:
                raise SimulationCampaignIntegrityError(
                    "Build Result sharing disagrees with Simulation Result reference"
                )
            bundle_path = build_root / cast(str, bundle_binding["manifest_path"])
            _require_safe_parents(bundle_path, build_root)
            bundle_raw = _read_regular(bundle_path, limit=RECORD_MAX_BYTES)
            if (
                len(bundle_raw) != bundle_binding["manifest_bytes"]
                or _raw_digest(bundle_raw) != bundle_binding["manifest_sha256"]
            ):
                raise SimulationCampaignIntegrityError(
                    "Build Result does not authenticate its Simulator Bundle"
                )
            bundle = decode_simulator_bundle(bundle_raw)
            if (
                bundle.document["bundle_id"] != bundle_binding["bundle_id"]
                or bundle.document["sharing"] != bundle_binding["sharing"]
                or bundle.document["artifacts"] != bundle_binding["artifacts"]
            ):
                raise SimulationCampaignIntegrityError(
                    "Simulator Bundle disagrees with its Build Result"
                )
            for artifact in cast(
                tuple[Mapping[str, object], ...], bundle.document["artifacts"]
            ):
                self._authenticate_reference(build_root, artifact)
        return build_reference, build_result

    def _verify_snapshot_chain(
        self,
        attempt_directory: Path,
        simulation: Mapping[str, object],
        build_reference: Mapping[str, object],
        build_result: BundleBuildResult,
    ) -> None:
        snapshot_binding = simulation["executable_snapshot"]
        if isinstance(snapshot_binding, Mapping):
            manifest_reference = cast(
                Mapping[str, object], snapshot_binding["manifest"]
            )
            snapshot_path = attempt_directory / cast(str, manifest_reference["path"])
            snapshot = decode_executable_snapshot(
                _read_regular(snapshot_path, limit=RECORD_MAX_BYTES)
            )
            if (
                snapshot.document["attempt_id"] != simulation["attempt_id"]
                or snapshot.document["bundle_id"] != simulation["bundle_id"]
                or snapshot.document["build_result"] != build_reference
                or snapshot.document["inventory_sha256"]
                != snapshot_binding["pre_launch_sha256"]
                or snapshot.document["inventory_sha256"]
                != snapshot_binding["post_exit_sha256"]
            ):
                raise SimulationCampaignIntegrityError(
                    "Executable Snapshot identity disagrees with Simulation Result"
                )
            bundle_binding = build_result.document["bundle"]
            if not isinstance(bundle_binding, Mapping) or (
                snapshot_binding["bundle_manifest_sha256"]
                != bundle_binding["manifest_sha256"]
            ):
                raise SimulationCampaignIntegrityError(
                    "Executable Snapshot does not bind its Simulator Bundle"
                )
            for artifact in cast(
                tuple[Mapping[str, object], ...], snapshot.document["artifacts"]
            ):
                self._authenticate_reference(snapshot_path.parent, artifact)

    @staticmethod
    def _authenticate_reference(
        root: Path, reference: Mapping[str, object]
    ) -> None:
        path = root / cast(str, reference["path"])
        _require_safe_parents(path, root)
        size, digest = _stream_identity(path)
        if size != reference["bytes"] or digest != reference["sha256"]:
            raise SimulationCampaignIntegrityError(
                f"result evidence reference does not authenticate {path}"
            )

    def _publish(
        self, path: Path, value: _T, encoder: Callable[[_T], bytes]
    ) -> Path:
        _require_safe_parents(path, self.root)
        _create_immutable(path, encoder(value))
        return path

    def scan(self) -> CampaignRecovery:
        manifest = self.load_manifest()
        expected_manifest = self.manifest_sha256()
        expected_workload = cast(Mapping[str, str], manifest.document["fingerprints"])[
            "workload_sha256"
        ]
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        recovered = tuple(
            self._scan_item(
                cast(str, item["work_item_id"]), expected_manifest, expected_workload
            )
            for item in items
        )
        return CampaignRecovery(recovered)

    def _scan_item(
        self, work_item_id: str, manifest_sha256: str, workload_sha256: str
    ) -> WorkItemRecovery:
        directory = self.work_item_directory(work_item_id)
        attempts_root = directory / "attempts"
        attempts = self._attempt_directories(attempts_root)
        decoded_attempts: list[SimulationAttempt] = []
        for ordinal, attempt_directory in enumerate(attempts, start=1):
            attempt_path = attempt_directory / "attempt.json"
            if not attempt_path.exists() and not _is_link(attempt_path):
                continue
            attempt = decode_simulation_attempt(
                _read_regular(attempt_path, limit=RECORD_MAX_BYTES)
            )
            document = attempt.document
            if (
                document["work_item_id"] != work_item_id
                or document["attempt_ordinal"] != ordinal
                or document["manifest_sha256"] != manifest_sha256
                or document["workload_sha256"] != workload_sha256
            ):
                raise SimulationCampaignIntegrityError(
                    f"attempt identity disagrees for {work_item_id} ordinal {ordinal}"
                )
            decoded_attempts.append(attempt)
        result_path = directory / "result.json"
        if result_path.exists() or _is_link(result_path):
            result = decode_simulation_result(_read_regular(result_path, limit=RECORD_MAX_BYTES))
            document = result.document
            if (
                document["work_item_id"] != work_item_id
                or document["manifest_sha256"] != manifest_sha256
                or document["workload_sha256"] != workload_sha256
            ):
                raise SimulationCampaignIntegrityError(
                    f"result identity disagrees for {work_item_id}"
                )
            if not decoded_attempts or decoded_attempts[-1].document["attempt_ordinal"] != len(
                attempts
            ):
                raise SimulationCampaignIntegrityError("result has no published attempt")
            final_attempt = decoded_attempts[-1].document
            if (
                document["attempt_id"] != final_attempt["attempt_id"]
                or document["attempt_ordinal"] != final_attempt["attempt_ordinal"]
            ):
                raise SimulationCampaignIntegrityError(
                    "result does not bind the final published attempt"
                )
            self.verify_result_evidence(attempts[-1], result)
            return WorkItemRecovery(work_item_id, "complete", len(attempts), result)
        state = "interrupted" if attempts else "pending"
        return WorkItemRecovery(work_item_id, state, len(attempts), None)

    @staticmethod
    def _attempt_directories(root: Path) -> tuple[Path, ...]:
        if not root.exists():
            return ()
        if _is_link(root) or not root.is_dir():
            raise SimulationCampaignIntegrityError(f"invalid attempts directory: {root}")
        children = sorted(root.iterdir(), key=lambda item: item.name)
        for ordinal, child in enumerate(children, start=1):
            prefix = f"{ordinal:04d}-"
            if not child.name.startswith(prefix) or _is_link(child) or not child.is_dir():
                raise SimulationCampaignIntegrityError(f"invalid attempt directory: {child}")
        return tuple(children)

    def regenerate_summary(self) -> Mapping[str, object]:
        manifest = self.load_manifest()
        recovery = self.scan()
        grades = tuple(
            cast(str, item.result.document["grade"])
            for item in recovery.items
            if item.result is not None
        )
        aggregate = _aggregate_grade(grades, complete=not recovery.pending and not recovery.interrupted)
        document: dict[str, object] = {
            "$schema": _SUMMARY_SCHEMA,
            "campaign_id": manifest.document["campaign_id"],
            "manifest_sha256": self.manifest_sha256(),
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
            raise SimulationCampaignIntegrityError("summary exceeds size ceiling")
        _replace_projection(self.summary_path, raw)
        return json.loads(raw)


def _aggregate_grade(grades: tuple[str, ...], *, complete: bool) -> str:
    if "error" in grades:
        return "error"
    if "fail" in grades:
        return "fail"
    if not complete or "inconclusive" in grades:
        return "inconclusive"
    return "pass"


def _stream_identity(path: Path) -> tuple[int, str]:
    if _is_link(path) or not path.is_file():
        raise SimulationCampaignIntegrityError(f"evidence is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


def _raw_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["CampaignRecovery", "CampaignStore", "WorkItemRecovery"]
