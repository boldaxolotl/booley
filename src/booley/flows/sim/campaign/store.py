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
import re
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_finite_number,
    require_int,
)
from booley.flows.sim.campaign_durability import (
    durable_create,
    durable_directory,
    fsync_directory,
)
from booley.runtime.file_lock import nonblocking_file_lock
from booley.runtime.regular_file import open_regular_nofollow

from .codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SUMMARY_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
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
    SimulatorBundle,
)

_T = TypeVar("_T", bound=SimulationCampaignDocument)
_SUMMARY_SCHEMA = "booley.simulation-campaign-summary/v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COMMON_BUILD_FIELDS = (
    "campaign_id",
    "manifest_sha256",
    "workload_sha256",
    "build_variant_id",
)


def _manifest_sha256(manifest: SimulationCampaignManifest) -> str:
    raw = encode_simulation_campaign_manifest(manifest)
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


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


@dataclass(frozen=True, slots=True)
class SharedBuildRecovery:
    """One fully authenticated settled shared build for an invocation."""

    directory: Path
    attempt: BundleBuildAttempt
    result: BundleBuildResult
    bundle: SimulatorBundle | None
    build_execution: Mapping[str, object] | None


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_safe_parents(path: Path, root: Path) -> None:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"campaign path escapes store: {path}") from exc
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
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
    descriptor: int | None = None
    try:
        descriptor = open_regular_nofollow(path)
        initial = _regular_identity(os.fstat(descriptor), path, limit)
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise SimulationCampaignIntegrityError(
                f"authoritative file exceeds size ceiling: {path}"
            )
        final = _regular_identity(os.fstat(descriptor), path, limit)
        current = _regular_identity(path.stat(follow_symlinks=False), path, limit)
        if initial != final or final != current or len(raw) != final[2]:
            raise SimulationCampaignIntegrityError(
                f"authoritative file changed during read: {path}"
            )
        return bytes(raw)
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot read authoritative file {path}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _regular_identity(
    info: os.stat_result, path: Path, limit: int
) -> tuple[int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode):
        raise SimulationCampaignIntegrityError(f"authoritative path is not a file: {path}")
    if info.st_nlink != 1:
        raise SimulationCampaignIntegrityError(
            f"authoritative file has multiple filesystem links: {path}"
        )
    if info.st_size > limit:
        raise SimulationCampaignIntegrityError(f"authoritative file exceeds size ceiling: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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
        return _manifest_sha256(manifest)

    def load_manifest(self) -> SimulationCampaignManifest:
        _require_safe_parents(self.manifest_path, self.root)
        return decode_simulation_campaign_manifest(
            _read_regular(self.manifest_path, limit=MANIFEST_MAX_BYTES)
        )

    def load_summary(self) -> Mapping[str, object]:
        """Load the bounded retention fields from the existing summary projection."""
        _require_safe_parents(self.summary_path, self.root)
        raw = _read_regular(self.summary_path, limit=SUMMARY_MAX_BYTES)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SimulationCampaignIntegrityError("campaign summary is invalid JSON") from exc
        if not isinstance(document, dict):
            raise SimulationCampaignIntegrityError("campaign summary is not an object")
        completed = document.get("completed")
        if (
            document.get("$schema") != _SUMMARY_SCHEMA
            or type(document.get("complete")) is not bool
            or not isinstance(completed, list)
            or any(not isinstance(item, str) or not item for item in completed)
            or len(set(completed)) != len(completed)
        ):
            raise SimulationCampaignIntegrityError("campaign summary retention fields are invalid")
        return document

    def manifest_sha256(self) -> str:
        return _manifest_sha256(self.load_manifest())

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Fail immediately when another scheduler owns this campaign."""
        _require_safe_parents(self.root, self.root)
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
                return self._work_item_directory(item)
        raise SimulationCampaignIntegrityError(f"unknown work item {work_item_id!r}")

    def _work_item_directory(self, item: Mapping[str, object]) -> Path:
        work_item_id = cast(str, item["work_item_id"])
        ordinal = cast(int, item["ordinal"]) + 1
        digest = work_item_id.rsplit(":", 1)[-1]
        return self.root / "work-items" / f"{ordinal:04d}-{digest}"

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

    def allocate_build_attempt_directory(
        self, build_variant_id: str, build_attempt_id: str
    ) -> tuple[int, Path]:
        """Allocate one append-only campaign-owned shared build attempt."""
        digest = _variant_digest(build_variant_id)
        attempts = self.root / "build-variants" / digest / "attempts"
        durable_directory(attempts)
        existing = self._attempt_directories(attempts)
        ordinal = len(existing) + 1
        if ordinal > 10_000:
            raise SimulationCampaignIntegrityError("shared build attempt ceiling exceeded")
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

    def recover_shared_build(
        self, build_variant_id: str, producer_invocation_id: int
    ) -> SharedBuildRecovery | None:
        """Return one fully authenticated settled build for this invocation."""
        digest = _variant_digest(build_variant_id)
        attempts = self._attempt_directories(self.root / "build-variants" / digest / "attempts")
        expected = self._shared_expected(build_variant_id)
        recovered: list[SharedBuildRecovery] = []
        for ordinal, directory in enumerate(attempts, start=1):
            candidate = self._recover_shared_attempt(
                directory, ordinal, producer_invocation_id, expected
            )
            if candidate is not None:
                recovered.append(candidate)
        if len(recovered) > 1:
            raise SimulationCampaignIntegrityError(
                "multiple settled shared builds exist for one variant/invocation"
            )
        return recovered[0] if recovered else None

    def _shared_expected(self, build_variant_id: str) -> Mapping[str, object]:
        manifest = self.load_manifest()
        workload = cast(Mapping[str, object], manifest.document["workload"])
        fingerprints = cast(Mapping[str, str], manifest.document["fingerprints"])
        variants = cast(tuple[Mapping[str, object], ...], manifest.document["build_variants"])
        if not any(item["build_variant_id"] == build_variant_id for item in variants):
            raise SimulationCampaignIntegrityError("shared build variant is not in manifest")
        eda = cast(Mapping[str, str], workload["eda"])
        return {
            "campaign_id": manifest.document["campaign_id"],
            "manifest_sha256": self.manifest_sha256(),
            "workload_sha256": fingerprints["workload_sha256"],
            "build_variant_id": build_variant_id,
            "tool_provenance": {
                "eda_kind": eda["kind"],
                "eda_version": eda["version"],
                "adapter_contract_version": workload["adapter_contract_version"],
            },
        }

    def _recover_shared_attempt(
        self, directory, ordinal, invocation_id, expected
    ) -> SharedBuildRecovery | None:
        attempt_path = directory / "build-attempt.json"
        if not attempt_path.exists():
            return None
        attempt = decode_bundle_build_attempt(_read_regular(attempt_path, limit=RECORD_MAX_BYTES))
        _validate_shared_attempt(attempt, ordinal, expected)
        result_path = directory / "build-result.json"
        if not result_path.exists():
            return None
        result = decode_bundle_build_result(_read_regular(result_path, limit=RECORD_MAX_BYTES))
        self._validate_shared_result(directory, attempt, result, expected)
        if attempt.document["producer_invocation_id"] != invocation_id:
            return None
        if result.document["state"] == "infrastructure_error":
            return None
        bundle, execution = self._shared_result_evidence(directory, attempt, result, expected)
        return SharedBuildRecovery(directory, attempt, result, bundle, execution)

    def _validate_shared_result(self, directory, attempt, result, expected) -> None:
        document = result.document
        if any(document[field] != expected[field] for field in _COMMON_BUILD_FIELDS):
            raise SimulationCampaignIntegrityError("shared Build Result identity disagrees")
        reference = cast(Mapping[str, object], document["build_attempt"])
        attempt_id = attempt.document["build_attempt_id"]
        if (
            reference["build_attempt_id"] != attempt_id
            or reference["owner"] != attempt_id
            or reference["kind"] != "bundle_build_attempt"
            or reference["path"] != "build-attempt.json"
        ):
            raise SimulationCampaignIntegrityError(
                "shared Build Result attempt reference disagrees"
            )
        self._authenticate_reference(directory, reference)

    def _shared_result_evidence(self, directory, attempt, result, expected):
        document = result.document
        for reference in cast(tuple[Mapping[str, object], ...], document["evidence"]):
            if reference["owner"] != attempt.document["build_attempt_id"]:
                raise SimulationCampaignIntegrityError("shared build evidence owner disagrees")
            self._authenticate_reference(directory, reference)
        if document["state"] == "design_failure":
            return None, None
        bundle = self._load_shared_bundle(directory, attempt, result, expected)
        execution = _load_build_execution(directory, document)
        return bundle, execution

    def _load_shared_bundle(self, directory, attempt, result, expected) -> SimulatorBundle:
        binding = cast(Mapping[str, object], result.document["bundle"])
        path = directory / cast(str, binding["manifest_path"])
        raw = _read_regular(path, limit=RECORD_MAX_BYTES)
        if len(raw) != binding["manifest_bytes"] or _raw_digest(raw) != binding["manifest_sha256"]:
            raise SimulationCampaignIntegrityError("shared Build Result bundle digest disagrees")
        bundle = decode_simulator_bundle(raw)
        _validate_shared_bundle(bundle, binding, attempt, expected)
        for artifact in cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"]):
            self._authenticate_reference(directory, artifact)
        return bundle

    def publish_result(self, work_item_id: str, result: SimulationResult) -> Path:
        path = self.work_item_directory(work_item_id) / "result.json"
        return self._publish(path, result, encode_simulation_result)

    def latest_attempt(self, work_item_id: str) -> SimulationAttempt | None:
        """Load the final validated attempt for an interrupted work item."""
        attempts = self._attempt_directories(self.work_item_directory(work_item_id) / "attempts")
        if not attempts or not (attempts[-1] / "attempt.json").exists():
            return None
        return decode_simulation_attempt(
            _read_regular(attempts[-1] / "attempt.json", limit=RECORD_MAX_BYTES)
        )

    def verify_result_evidence(self, attempt_directory: Path, result: SimulationResult) -> None:
        """Authenticate private or shared build chains before result commit."""
        document = result.document
        attempt = decode_simulation_attempt(
            _read_regular(attempt_directory / "attempt.json", limit=RECORD_MAX_BYTES)
        )
        _validate_result_attempt_binding(attempt, result)
        references: list[Mapping[str, object]] = list(
            cast(tuple[Mapping[str, object], ...], document["evidence"])
        )
        snapshot = document["executable_snapshot"]
        if isinstance(snapshot, Mapping):
            references.append(cast(Mapping[str, object], snapshot["manifest"]))
        for binding in cast(tuple[Mapping[str, object], ...], document["runtime_inputs"]):
            references.append(cast(Mapping[str, object], binding["authoritative_copy"]))
        for reference in references:
            self._authenticate_reference(attempt_directory, reference)
        build_reference, build_result, bundle = self._verify_build_chain(
            attempt_directory, document, attempt
        )
        self._verify_snapshot_chain(
            attempt_directory,
            document,
            build_reference,
            build_result,
            bundle,
        )

    def _verify_build_chain(
        self,
        attempt_directory: Path,
        simulation: Mapping[str, object],
        attempt: SimulationAttempt,
    ) -> tuple[Mapping[str, object], BundleBuildResult, SimulatorBundle | None]:
        build_reference = cast(Mapping[str, object], simulation["build_result"])
        build_path = self._result_build_path(attempt_directory, build_reference)
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
        if build_document["build_variant_id"] != attempt.document["build_variant_id"]:
            raise SimulationCampaignIntegrityError(
                "Build Result variant disagrees with Simulation Attempt"
            )
        build_root, build_attempt = self._load_result_build_attempt(build_path, build_document)
        _validate_result_build_attempt(build_attempt, build_document, simulation, attempt)
        bundle = self._load_result_bundle(
            build_root, build_document, build_reference, simulation, attempt, build_attempt
        )
        return build_reference, build_result, bundle

    def _result_build_path(self, attempt_directory, reference) -> Path:
        root = self.root if reference["sharing"] == "shared_variant" else attempt_directory
        path = root / cast(str, reference["path"])
        _require_safe_parents(path, root)
        return path

    def _load_result_build_attempt(self, build_path, build_document):
        root = build_path.parent
        reference = cast(Mapping[str, object], build_document["build_attempt"])
        self._authenticate_reference(root, reference)
        attempt = decode_bundle_build_attempt(
            _read_regular(root / cast(str, reference["path"]), limit=RECORD_MAX_BYTES)
        )
        return root, attempt

    def _load_result_bundle(
        self, root, result, reference, simulation, attempt, build_attempt
    ) -> SimulatorBundle | None:
        binding = result["bundle"]
        if not isinstance(binding, Mapping):
            return None
        if binding["sharing"] != reference["sharing"]:
            raise SimulationCampaignIntegrityError(
                "Build Result sharing disagrees with Simulation Result reference"
            )
        path = root / cast(str, binding["manifest_path"])
        _require_safe_parents(path, root)
        raw = _read_regular(path, limit=RECORD_MAX_BYTES)
        if len(raw) != binding["manifest_bytes"] or _raw_digest(raw) != binding["manifest_sha256"]:
            raise SimulationCampaignIntegrityError("Build Result bundle digest disagrees")
        bundle = decode_simulator_bundle(raw)
        _validate_result_bundle_identity(bundle, binding, reference, simulation, attempt)
        for artifact in cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"]):
            self._authenticate_reference(root, artifact)
        _validate_bundle_scope(bundle, reference, attempt, build_attempt)
        return bundle

    def _verify_snapshot_chain(
        self,
        attempt_directory: Path,
        simulation: Mapping[str, object],
        build_reference: Mapping[str, object],
        build_result: BundleBuildResult,
        bundle: SimulatorBundle | None,
    ) -> None:
        snapshot_binding = simulation["executable_snapshot"]
        if isinstance(snapshot_binding, Mapping):
            manifest_reference = cast(Mapping[str, object], snapshot_binding["manifest"])
            snapshot_path = attempt_directory / cast(str, manifest_reference["path"])
            snapshot = decode_executable_snapshot(
                _read_regular(snapshot_path, limit=RECORD_MAX_BYTES)
            )
            _validate_snapshot_identity(snapshot, simulation, build_reference, snapshot_binding)
            bundle_binding = build_result.document["bundle"]
            if not isinstance(bundle_binding, Mapping) or (
                snapshot_binding["bundle_manifest_sha256"] != bundle_binding["manifest_sha256"]
            ):
                raise SimulationCampaignIntegrityError(
                    "Executable Snapshot does not bind its Simulator Bundle"
                )
            if bundle is None:
                raise SimulationCampaignIntegrityError(
                    "Executable Snapshot has no retained Simulator Bundle"
                )
            eligible = tuple(
                item
                for item in cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"])
                if item["kind"] != "runtime_input"
            )
            if snapshot.document["artifacts"] != eligible:
                raise SimulationCampaignIntegrityError(
                    "Executable Snapshot artifacts disagree with Simulator Bundle"
                )
            for artifact in cast(tuple[Mapping[str, object], ...], snapshot.document["artifacts"]):
                self._authenticate_reference(snapshot_path.parent, artifact)

    @staticmethod
    def _authenticate_reference(root: Path, reference: Mapping[str, object]) -> None:
        path = root / cast(str, reference["path"])
        _require_safe_parents(path, root)
        size, digest = _stream_identity(path)
        if size != reference["bytes"] or digest != reference["sha256"]:
            raise SimulationCampaignIntegrityError(
                f"result evidence reference does not authenticate {path}"
            )

    def _publish(self, path: Path, value: _T, encoder: Callable[[_T], bytes]) -> Path:
        _require_safe_parents(path, self.root)
        _create_immutable(path, encoder(value))
        return path

    def scan(self) -> CampaignRecovery:
        manifest = self.load_manifest()
        return self._scan_manifest(manifest, _manifest_sha256(manifest))

    def scan_validated(
        self,
        manifest: SimulationCampaignManifest,
        manifest_sha256: str,
    ) -> CampaignRecovery:
        """Scan state only when storage still contains an authenticated manifest."""
        stored = self.load_manifest()
        if stored != manifest or _manifest_sha256(stored) != manifest_sha256:
            raise SimulationCampaignIntegrityError(
                "campaign manifest changed after resume validation"
            )
        return self._scan_manifest(manifest, manifest_sha256)

    def _scan_manifest(
        self,
        manifest: SimulationCampaignManifest,
        manifest_sha256: str,
    ) -> CampaignRecovery:
        expected_workload = cast(Mapping[str, str], manifest.document["fingerprints"])[
            "workload_sha256"
        ]
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        recovered = tuple(
            self._scan_item(item, manifest_sha256, expected_workload) for item in items
        )
        return CampaignRecovery(recovered)

    def _scan_item(
        self,
        work_item: Mapping[str, object],
        manifest_sha256: str,
        workload_sha256: str,
    ) -> WorkItemRecovery:
        work_item_id = cast(str, work_item["work_item_id"])
        # ``scan`` already decoded the complete manifest.  Re-loading and
        # re-validating it once per item makes maximum-size campaigns quadratic.
        directory = self._work_item_directory(work_item)
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
            self._validate_child_attempt_binding(attempt_directory, document)
            decoded_attempts.append(attempt)
        result_path = directory / "result.json"
        if result_path.exists() or _is_link(result_path):
            result = self._scan_result(
                result_path,
                work_item_id,
                manifest_sha256,
                workload_sha256,
                attempts,
                decoded_attempts,
            )
            _validate_result_selection(work_item, result, attempts[-1])
            return WorkItemRecovery(work_item_id, "complete", len(attempts), result)
        state = "interrupted" if attempts else "pending"
        return WorkItemRecovery(work_item_id, state, len(attempts), None)

    def _validate_child_attempt_binding(
        self, attempt_directory: Path, attempt: Mapping[str, object]
    ) -> None:
        child_id = attempt["child_execution_id"]
        if child_id is None:
            return
        path = self.root / "child-executions" / "entries" / f"{child_id}.json"
        raw = _read_regular(path, limit=RECORD_MAX_BYTES)
        if _raw_digest(raw) != attempt["child_entry_sha256"]:
            raise SimulationCampaignIntegrityError(
                "Simulation Attempt child entry digest disagrees"
            )
        try:
            entry = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SimulationCampaignIntegrityError(
                "Simulation Attempt child entry is invalid JSON"
            ) from exc
        expected = {
            "child_execution_id": child_id,
            "campaign_id": attempt["campaign_id"],
            "manifest_sha256": attempt["manifest_sha256"],
            "work_item_id": attempt["work_item_id"],
            "attempt_id": attempt["attempt_id"],
            "attempt_ordinal": attempt["attempt_ordinal"],
            "attempt_relative_path": attempt_directory.relative_to(self.root).as_posix(),
        }
        if not isinstance(entry, dict) or any(
            entry.get(field) != value for field, value in expected.items()
        ):
            raise SimulationCampaignIntegrityError(
                "Simulation Attempt disagrees with its exact child entry"
            )

    def _scan_result(
        self,
        path: Path,
        work_item_id: str,
        manifest_sha256: str,
        workload_sha256: str,
        attempts: tuple[Path, ...],
        decoded_attempts: list[SimulationAttempt],
    ) -> SimulationResult:
        result = decode_simulation_result(_read_regular(path, limit=RECORD_MAX_BYTES))
        document = result.document
        if (
            document["work_item_id"] != work_item_id
            or document["manifest_sha256"] != manifest_sha256
            or document["workload_sha256"] != workload_sha256
        ):
            raise SimulationCampaignIntegrityError(f"result identity disagrees for {work_item_id}")
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
        return result

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
        manifest_sha256 = _manifest_sha256(manifest)
        recovery = self._scan_manifest(manifest, manifest_sha256)
        return self._regenerate_summary(manifest, manifest_sha256, recovery)

    def regenerate_summary_validated(
        self,
        manifest: SimulationCampaignManifest,
        manifest_sha256: str,
    ) -> Mapping[str, object]:
        """Regenerate the projection from one authenticated manifest snapshot."""
        recovery = self.scan_validated(manifest, manifest_sha256)
        return self._regenerate_summary(manifest, manifest_sha256, recovery)

    def _regenerate_summary(
        self,
        manifest: SimulationCampaignManifest,
        manifest_sha256: str,
        recovery: CampaignRecovery,
    ) -> Mapping[str, object]:
        grades = tuple(
            cast(str, item.result.document["grade"])
            for item in recovery.items
            if item.result is not None
        )
        aggregate = _aggregate_grade(
            grades, complete=not recovery.pending and not recovery.interrupted
        )
        document: dict[str, object] = {
            "$schema": _SUMMARY_SCHEMA,
            "campaign_id": manifest.document["campaign_id"],
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
            raise SimulationCampaignIntegrityError("summary exceeds size ceiling")
        _require_safe_parents(self.summary_path, self.root)
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


def _validate_result_selection(
    work_item: Mapping[str, object], result: SimulationResult, attempt_directory: Path
) -> None:
    """Bind terminal observation order and transport evidence to the manifest."""
    selection = cast(Mapping[str, object], work_item["selection"])
    names = cast(tuple[str, ...], selection["names"])
    observations = cast(tuple[Mapping[str, object], ...], result.document["observations"])
    observed = tuple(item["test"] for item in observations)
    kind = cast(str, work_item["kind"])
    if selection["kind"] == "named" and observed != names:
        raise SimulationCampaignIntegrityError(
            "result observations disagree with named work-item selection"
        )
    if selection["kind"] == "unfiltered":
        unnamed_failure = observed == (None,) and result.document["state"] in {
            "blocked_by_build",
            "setup_error",
            "timeout",
            "crash",
        }
        discovered = bool(observed) and all(isinstance(name, str) and name for name in observed)
        if not (unnamed_failure or discovered):
            raise SimulationCampaignIntegrityError(
                "unfiltered Cocotb result has invalid observation identity"
            )
    if result.document["state"] not in {"completed", "timeout", "crash"}:
        return
    evidence = cast(tuple[Mapping[str, object], ...], result.document["evidence"])
    expected_kind = "coverage_campaign_manifest" if kind == "coverage_aggregate" else None
    if kind == "cocotb_batch" and observed != (None,):
        expected_kind = "cocotb_results"
    if expected_kind is not None:
        matching = [item for item in evidence if item["kind"] == expected_kind]
        if len(matching) != 1 or matching[0]["owner"] != result.document["attempt_id"]:
            raise SimulationCampaignIntegrityError(
                f"{kind} result lacks exact authenticated transport evidence"
            )
        if kind == "cocotb_batch":
            _validate_cocotb_transport(attempt_directory, matching[0], observations)


def _validate_cocotb_transport(
    attempt_directory: Path,
    reference: Mapping[str, object],
    observations: tuple[Mapping[str, object], ...],
) -> None:
    path = attempt_directory / cast(str, reference["path"])
    raw = _read_regular(path, limit=RECORD_MAX_BYTES)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError("Cocotb transport is invalid JSON") from exc
    if canonical_json_bytes(document) != raw or not isinstance(document, dict):
        raise SimulationCampaignIntegrityError("Cocotb transport is not canonical JSON")
    if set(document) != {
        "$schema",
        "source_bytes",
        "source_sha256",
        "source_observations",
        "observations",
    }:
        raise SimulationCampaignIntegrityError("Cocotb transport fields are invalid")
    transported = document["observations"]
    if (
        document["$schema"] != "booley.cocotb-campaign-transport/v1"
        or not isinstance(document["source_bytes"], int)
        or document["source_bytes"] < 1
        or not isinstance(document["source_sha256"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", document["source_sha256"])
        or transported != [dict(item) for item in observations]
    ):
        raise SimulationCampaignIntegrityError(
            "Cocotb transport observations disagree with Simulation Result"
        )
    _validate_cocotb_source(document["source_observations"], observations)


def _validate_cocotb_source(
    source: object, observations: tuple[Mapping[str, object], ...]
) -> None:
    if not isinstance(source, list) or len(source) != len(observations):
        raise SimulationCampaignIntegrityError("Cocotb source transport count disagrees")
    for transported, observed in zip(source, observations, strict=True):
        if not isinstance(transported, dict) or set(transported) != {"test", "verdict", "detail"}:
            raise SimulationCampaignIntegrityError("Cocotb source transport is invalid")
        verdict_matches = _cocotb_source_verdict_matches(transported, observed)
        detail = cast(Mapping[str, object], observed["detail"])["reason"]
        if (
            transported["test"] != observed["test"]
            or not verdict_matches
            or (transported["detail"] and transported["detail"] != detail)
        ):
            raise SimulationCampaignIntegrityError("Cocotb source transport disagrees")


def _cocotb_source_verdict_matches(
    transported: Mapping[str, object], observed: Mapping[str, object]
) -> bool:
    verdict = transported["verdict"]
    functional = observed["functional"]
    if verdict == functional:
        return True
    if observed["execution"] == "timeout":
        return verdict == "fail"
    if verdict != "pass":
        return False
    return observed["assertions"] == "dirty" or functional == "inconclusive"


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


def _variant_digest(build_variant_id: str) -> str:
    prefix, separator, digest = build_variant_id.partition(":")
    if prefix != "variant" or separator != ":" or _HEX64.fullmatch(digest) is None:
        raise SimulationCampaignIntegrityError("invalid shared build variant identity")
    return digest


def _validate_shared_attempt(attempt, ordinal: int, expected) -> None:
    document = attempt.document
    if any(document[field] != expected[field] for field in _COMMON_BUILD_FIELDS):
        raise SimulationCampaignIntegrityError("shared Build Attempt identity disagrees")
    if (
        document["build_attempt_ordinal"] != ordinal
        or document["sharing"] != "shared_variant"
        or document["owner"] != {"work_item_id": None, "simulation_attempt_id": None}
        or document["tool_provenance"] != expected["tool_provenance"]
    ):
        raise SimulationCampaignIntegrityError("shared Build Attempt scope disagrees")


def _validate_shared_bundle(bundle, binding, attempt, expected) -> None:
    document = bundle.document
    if any(document[field] != expected[field] for field in _COMMON_BUILD_FIELDS):
        raise SimulationCampaignIntegrityError("shared Simulator Bundle identity disagrees")
    if (
        binding["sharing"] != "shared_variant"
        or document["build_attempt_id"] != attempt.document["build_attempt_id"]
        or document["bundle_id"] != binding["bundle_id"]
        or document["sharing"] != "shared_variant"
        or document["owner"] != {"work_item_id": None, "simulation_attempt_id": None}
        or document["tool_provenance"] != expected["tool_provenance"]
        or document["artifacts"] != binding["artifacts"]
    ):
        raise SimulationCampaignIntegrityError("shared Simulator Bundle binding disagrees")


def _load_build_execution(directory: Path, result: Mapping[str, object]) -> Mapping[str, object]:
    references = cast(tuple[Mapping[str, object], ...], result["evidence"])
    matches = [item for item in references if item["kind"] == "simulation_build_execution"]
    if len(matches) != 1:
        raise SimulationCampaignIntegrityError("ready shared build lacks exact execution evidence")
    raw = _read_regular(directory / cast(str, matches[0]["path"]), limit=RECORD_MAX_BYTES)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError("build execution evidence is invalid JSON") from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise SimulationCampaignIntegrityError("build execution evidence is not canonical")
    if set(document) != {"$schema", "process", "build"} or document["$schema"] != (
        "booley.simulation-build-execution/v1"
    ):
        raise SimulationCampaignIntegrityError("build execution evidence schema disagrees")
    _validate_build_execution_document(document)
    return cast(Mapping[str, object], document)


def _validate_build_execution_document(document: Mapping[str, object]) -> None:
    process = document["process"]
    build = document["build"]
    if not isinstance(process, dict) or set(process) != {
        "returncode",
        "stdout",
        "stderr",
        "timed_out",
        "duration_s",
        "dispatched_unix",
        "peak_rss_mb",
        "oom_kill_delta",
    }:
        raise SimulationCampaignIntegrityError("build process evidence fields disagree")
    if not isinstance(build, dict) or set(build) != {
        "ran",
        "verdict",
        "failure_kind",
        "elapsed_s",
        "output",
        "returncode",
        "timed_out",
        "peak_rss_mb",
        "oom_kill_delta",
        "terminal_record",
        "reason",
        "cache_decision",
    }:
        raise SimulationCampaignIntegrityError("normalized build evidence fields disagree")
    _validate_process_values(process)
    _validate_build_values(build)


def _validate_process_values(process: Mapping[str, object]) -> None:
    try:
        require_int(process["returncode"], field="build process returncode")
        require_int(process["oom_kill_delta"], field="build process OOM delta")
        require_bool_value(process["timed_out"], field="build process timed_out")
        for field in ("duration_s", "dispatched_unix"):
            require_finite_number(process[field], field=f"build process {field}")
        peak = process["peak_rss_mb"]
        if peak is not None:
            require_finite_number(peak, field="build process peak_rss_mb")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError("build process evidence types disagree") from exc
    if not isinstance(process["stdout"], str) or not isinstance(process["stderr"], str):
        raise SimulationCampaignIntegrityError("build process evidence types disagree")


def _validate_build_values(build: Mapping[str, object]) -> None:
    try:
        require_int(build["returncode"], field="normalized build returncode")
        require_bool_value(build["timed_out"], field="normalized build timed_out")
        require_bool_value(build["terminal_record"], field="normalized build terminal_record")
        require_int(build["oom_kill_delta"], field="normalized build OOM delta")
        for field in ("elapsed_s", "peak_rss_mb"):
            if build[field] is not None:
                require_finite_number(build[field], field=f"normalized build {field}")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError("normalized build measurement is invalid") from exc
    if (
        build["ran"] is not True
        or build["verdict"] != "pass"
        or build["failure_kind"] is not None
        or build["returncode"] != 0
        or build["timed_out"] is not False
        or any(
            not isinstance(build[field], str) for field in ("output", "reason", "cache_decision")
        )
    ):
        raise SimulationCampaignIntegrityError("normalized build evidence is not successful")


def _validate_result_attempt_binding(attempt: SimulationAttempt, result: SimulationResult) -> None:
    left, right = attempt.document, result.document
    fields = (
        "campaign_id",
        "manifest_sha256",
        "workload_sha256",
        "work_item_id",
        "attempt_id",
        "attempt_ordinal",
        "producer_invocation_id",
    )
    if any(left[field] != right[field] for field in fields):
        raise SimulationCampaignIntegrityError(
            "Simulation Result identity disagrees with Simulation Attempt"
        )


def _validate_result_build_attempt(build, result, simulation, attempt) -> None:
    document = build.document
    reference = cast(Mapping[str, object], result["build_attempt"])
    if (
        document["build_attempt_id"] != reference["build_attempt_id"]
        or document["campaign_id"] != simulation["campaign_id"]
        or document["manifest_sha256"] != simulation["manifest_sha256"]
        or document["workload_sha256"] != simulation["workload_sha256"]
        or document["build_variant_id"] != attempt.document["build_variant_id"]
    ):
        raise SimulationCampaignIntegrityError(
            "Bundle Build Attempt identity disagrees with Simulation Result"
        )


def _validate_bundle_scope(bundle, reference, attempt, build_attempt) -> None:
    document = bundle.document
    private = reference["sharing"] == "private_work_item"
    expected_owner = (
        {
            "work_item_id": attempt.document["work_item_id"],
            "simulation_attempt_id": attempt.document["attempt_id"],
        }
        if private
        else {"work_item_id": None, "simulation_attempt_id": None}
    )
    if (
        document["owner"] != expected_owner
        or document["tool_provenance"] != build_attempt.document["tool_provenance"]
        or build_attempt.document["sharing"] != reference["sharing"]
        or build_attempt.document["owner"] != expected_owner
    ):
        raise SimulationCampaignIntegrityError(
            "Simulator Bundle scope/provenance disagrees with build chain"
        )


def _validate_result_bundle_identity(bundle, binding, reference, simulation, attempt) -> None:
    document = bundle.document
    if (
        document["bundle_id"] != binding["bundle_id"]
        or document["sharing"] != binding["sharing"]
        or document["artifacts"] != binding["artifacts"]
        or document["campaign_id"] != simulation["campaign_id"]
        or document["manifest_sha256"] != simulation["manifest_sha256"]
        or document["workload_sha256"] != simulation["workload_sha256"]
        or document["build_variant_id"] != attempt.document["build_variant_id"]
        or document["build_attempt_id"] != reference["build_attempt_id"]
    ):
        raise SimulationCampaignIntegrityError("Simulator Bundle disagrees with its Build Result")


def _validate_snapshot_identity(snapshot, simulation, build_reference, binding) -> None:
    document = snapshot.document
    if (
        document["attempt_id"] != simulation["attempt_id"]
        or document["campaign_id"] != simulation["campaign_id"]
        or document["manifest_sha256"] != simulation["manifest_sha256"]
        or document["workload_sha256"] != simulation["workload_sha256"]
        or document["work_item_id"] != simulation["work_item_id"]
        or document["bundle_id"] != simulation["bundle_id"]
        or document["build_result"] != build_reference
        or document["inventory_sha256"] != binding["pre_launch_sha256"]
        or document["inventory_sha256"] != binding["post_exit_sha256"]
    ):
        raise SimulationCampaignIntegrityError(
            "Executable Snapshot identity disagrees with Simulation Result"
        )


__all__ = ["CampaignRecovery", "CampaignStore", "SharedBuildRecovery", "WorkItemRecovery"]
