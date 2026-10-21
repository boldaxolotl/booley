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
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_str_value,
)
from booley.flows.sim.adapter_transport import (
    AdapterTransportError,
    AdapterTransportIdentity,
    read_adapter_result,
)
from booley.flows.sim.build_session import CacheDecision
from booley.flows.sim.campaign_durability import (
    durable_create,
    durable_directory,
    fsync_directory,
)
from booley.runtime.file_lock import nonblocking_file_lock
from booley.runtime.filesystem_utils import replace_file
from booley.runtime.regular_file import open_regular_nofollow

from .codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SUMMARY_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
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
from .planning import manifest_digest
from .pre_sim_evidence import PreSimFiring, PreSimFiringDecoder, _reference
from .test_run_binding import observation_test_runs

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


@dataclass(frozen=True, slots=True)
class _PreSimReadSnapshot:
    attempt: SimulationAttempt
    firings: tuple[PreSimFiring, ...]


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
        replace_file(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_regular(path: Path, *, limit: int) -> bytes:
    if _is_link(path):
        raise SimulationCampaignIntegrityError(f"authoritative path is a link: {path}")
    descriptor: int | None = None
    current_descriptor: int | None = None
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
        current_descriptor = open_regular_nofollow(path)
        current = _regular_identity(os.fstat(current_descriptor), path, limit)
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
        if current_descriptor is not None:
            os.close(current_descriptor)
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
        self._authenticated_files: set[Path] = set()

    @property
    def retention_files(self) -> tuple[Path, ...]:
        """Files this store read or hashed, in stable order."""
        return tuple(sorted(self._authenticated_files))

    def _read(self, path: Path, *, limit: int) -> bytes:
        raw = _read_regular(path, limit=limit)
        self._authenticated_files.add(path.absolute())
        return raw

    def _identity(self, path: Path) -> tuple[int, str]:
        identity = _stream_identity(path)
        self._authenticated_files.add(path.absolute())
        return identity

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
            self._read(self.manifest_path, limit=MANIFEST_MAX_BYTES)
        )

    def load_summary(self) -> Mapping[str, object]:
        """Load the bounded retention fields from the existing summary projection."""
        _require_safe_parents(self.summary_path, self.root)
        raw = self._read(self.summary_path, limit=SUMMARY_MAX_BYTES)
        try:
            document = require_dict(json.loads(raw), field="campaign summary")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SimulationCampaignIntegrityError("campaign summary is invalid JSON") from exc
        except BoundaryError as exc:
            raise SimulationCampaignIntegrityError("campaign summary is not an object") from exc
        try:
            schema = require_str_value(document.get("$schema"), field="campaign summary schema")
            require_bool_value(document.get("complete"), field="campaign summary complete")
            completed = tuple(
                require_str_value(item, field="campaign summary completed item")
                for item in require_list(
                    document.get("completed"), field="campaign summary completed"
                )
            )
        except BoundaryError as exc:
            raise SimulationCampaignIntegrityError(
                "campaign summary retention fields are invalid"
            ) from exc
        if schema != _SUMMARY_SCHEMA or len(set(completed)) != len(completed):
            raise SimulationCampaignIntegrityError("campaign summary retention fields are invalid")
        return document

    def manifest_sha256(self) -> str:
        return _manifest_sha256(self.load_manifest())

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Fail immediately when another scheduler owns this campaign."""
        _require_safe_parents(self.root, self.root)
        if not self.root.is_dir():
            raise SimulationCampaignIntegrityError("Simulation Campaign directory disappeared")
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
        attempt = decode_bundle_build_attempt(self._read(attempt_path, limit=RECORD_MAX_BYTES))
        _validate_shared_attempt(attempt, ordinal, expected)
        result_path = directory / "build-result.json"
        if not result_path.exists():
            return None
        result = decode_bundle_build_result(self._read(result_path, limit=RECORD_MAX_BYTES))
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
        raw = self._read(path, limit=RECORD_MAX_BYTES)
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
        _require_safe_parents(path, self.root)
        if len(raw) > MANIFEST_MAX_BYTES:
            raise SimulationCampaignIntegrityError("Pre-Sim Commands evidence exceeds byte limit")
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
            raise SimulationCampaignIntegrityError("invalid Pre-Sim Commands evidence directory")
        paths = sorted(root.glob("*.json"))
        if len(paths) > 4096:
            raise SimulationCampaignIntegrityError("too many Pre-Sim Commands firings")
        return paths

    def _read_pre_sim_snapshot(self, directory, attempt, manifest=None, item=None):
        paths = self._pre_sim_paths(directory)
        firings = []
        if paths:
            manifest = manifest or self.load_manifest()
            item = item or next(
                item
                for item in manifest.document["work_items"]
                if item["work_item_id"] == attempt.document["work_item_id"]
            )
            decoder = PreSimFiringDecoder(manifest)
            for path in paths:
                _require_safe_parents(path, directory)
                raw = self._read(path, limit=MANIFEST_MAX_BYTES)
                firings.append(
                    PreSimFiring(
                        self.manifest_path,
                        path,
                        MappingProxyType(_reference(path, raw, directory.name.split("-", 1)[1])),
                        decoder.decode(raw, item, attempt),
                        False,
                    )
                )
        return _PreSimReadSnapshot(attempt, tuple(firings))

    def inspect_pre_sim_firings(
        self, manifest: SimulationCampaignManifest | None = None
    ) -> tuple[PreSimFiring, ...]:
        """Return firings from the same authenticated snapshot used by the full scan."""
        manifest = manifest or self.load_manifest()
        firings: list[PreSimFiring] = []
        self._scan_manifest(manifest, _manifest_sha256(manifest), pre_sim_receiver=firings.extend)
        return tuple(firings)

    def _reread_prior_attempt(
        self,
        manifest: SimulationCampaignManifest,
        items: Mapping[str, Mapping[str, object]],
        directory: Path,
        owned: list[PreSimFiring],
    ) -> dict[tuple[str, str, str, int], PreSimFiring]:
        """Check one attempt once and return the fresh firings matching its prior ones."""
        digest = _manifest_sha256(manifest)
        item = items.get(owned[0].key[1])
        if any(firing.document["manifest_sha256"] != digest for firing in owned):
            raise SimulationCampaignIntegrityError("prior hook manifest changed")
        if item is None:
            raise SimulationCampaignIntegrityError("prior hook manifest owner changed")
        _require_safe_parents(directory, self.root)
        if directory.parent != self._work_item_directory(item) / "attempts":
            raise SimulationCampaignIntegrityError("prior hook attempt owner changed")
        snapshot = self._read_attempt_pre_sim_snapshot(manifest, item, directory, None)
        fresh = {candidate.key: candidate for candidate in snapshot.firings} if snapshot else {}
        for firing in owned:
            match = fresh.get(firing.key)
            if match is None or (match.path, match.reference) != (firing.path, firing.reference):
                raise SimulationCampaignIntegrityError("prior hook reference changed")
        return {firing.key: fresh[firing.key] for firing in owned}

    def read_pre_sim_firings(
        self, manifest: SimulationCampaignManifest | None = None
    ) -> tuple[PreSimFiring, ...]:
        """Authenticate every retained attempt, including interrupted and older attempts."""
        return tuple(
            firing
            for snapshot in self._read_pre_sim_snapshots(manifest).values()
            for firing in snapshot.firings
        )

    def _read_pre_sim_snapshots(self, manifest=None) -> dict[Path, _PreSimReadSnapshot]:
        manifest = manifest or self.load_manifest()
        snapshots = {}
        for item in cast(tuple[Mapping[str, object], ...], manifest.document["work_items"]):
            work_root = self._work_item_directory(item)
            terminal = None
            result_path = work_root / "result.json"
            if result_path.exists() or result_path.is_symlink():
                terminal = decode_simulation_result(
                    self._read(result_path, limit=RECORD_MAX_BYTES)
                )
            for directory in self._attempt_directories(work_root / "attempts"):
                snapshot = self._read_attempt_pre_sim_snapshot(manifest, item, directory, terminal)
                if snapshot is not None:
                    snapshots[directory] = snapshot
        return snapshots

    def _read_attempt_pre_sim_snapshot(self, manifest, item, directory, terminal):
        if not (directory / "attempt.json").exists():
            return None
        attempt = self.read_attempt(directory)
        if (
            directory.name
            != f"{attempt.document['attempt_ordinal']:04d}-{attempt.document['attempt_id']}"
        ):
            raise SimulationCampaignIntegrityError("hook attempt directory owner disagrees")
        snapshot = self._read_pre_sim_snapshot(directory, attempt, manifest, item)
        completed = (
            terminal is not None
            and terminal.document["attempt_id"] == attempt.document["attempt_id"]
        )
        for ordinal, firing in enumerate(snapshot.firings, start=1):
            if firing.document["ordinal"] != ordinal or firing.path.name != f"{ordinal:04d}.json":
                raise SimulationCampaignIntegrityError("Pre-Sim Commands ordinal disagrees")
        if completed and [
            reference
            for reference in terminal.document["evidence"]
            if reference["kind"] == "pre_sim_commands"
        ] != [firing.reference for firing in snapshot.firings]:
            raise SimulationCampaignIntegrityError("Pre-Sim Commands result references disagree")
        if completed and snapshot.firings:
            _validate_result_attempt_binding(attempt, terminal)
            snapshot = replace(
                snapshot, firings=tuple(replace(f, terminal=True) for f in snapshot.firings)
            )
        return snapshot

    def _authenticate_reference(self, root: Path, reference: Mapping[str, object]) -> None:
        path = root / cast(str, reference["path"])
        _require_safe_parents(path, root)
        size, digest = self._identity(path)
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
        *,
        pre_sim_receiver: Callable[[tuple[PreSimFiring, ...]], None] | None = None,
    ) -> CampaignRecovery:
        snapshots = self._read_pre_sim_snapshots(manifest)
        if pre_sim_receiver is not None:
            pre_sim_receiver(tuple(f for snapshot in snapshots.values() for f in snapshot.firings))
        expected_workload = cast(Mapping[str, str], manifest.document["fingerprints"])[
            "workload_sha256"
        ]
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        target = cast(Mapping[str, str], manifest.document["target"])
        target_identity = f"{target['vlnv']}#{target['name']}"
        recovered = tuple(
            self._scan_item(item, manifest_sha256, expected_workload, target_identity, snapshots)
            for item in items
        )
        return CampaignRecovery(recovered)

    def _scan_item(
        self,
        work_item: Mapping[str, object],
        manifest_sha256: str,
        workload_sha256: str,
        target_identity: str,
        snapshots: dict[Path, _PreSimReadSnapshot],
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
            snapshot = snapshots.get(attempt_directory)
            attempt = (
                snapshot.attempt if snapshot is not None else self.read_attempt(attempt_directory)
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
            _validate_result_selection(
                work_item, result, attempts[-1], target_identity=target_identity
            )
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
        raw = self._read(path, limit=RECORD_MAX_BYTES)
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
        result = decode_simulation_result(self._read(path, limit=RECORD_MAX_BYTES))
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


_PRE_SIM_DAG_BOUND = 4096


def collect_pre_sim_firings(*manifest_paths: Path) -> tuple[PreSimFiring, ...]:
    """Read every root's prerequisite DAG once and deduplicate by full firing identity.

    The key ``(campaign_id, work_item_id, attempt_id, ordinal)`` keeps Candidate
    and Cycle Count baseline firings distinct.  Each distinct manifest path is
    scanned once.  Prerequisite references repeated by many work items count
    once per ancestor chain toward the bound.
    """
    pending = [(Path(path).absolute(), None, frozenset()) for path in reversed(manifest_paths)]
    queued: set[tuple[Path, str, frozenset[str]]] = set()
    visited: set[Path] = set()
    firings = {}
    allowed_pops = len(manifest_paths) + _PRE_SIM_DAG_BOUND
    while pending:
        if allowed_pops == 0:
            raise SimulationCampaignIntegrityError("prerequisite hook evidence exceeds DAG bound")
        allowed_pops -= 1
        path, expected, ancestors = pending.pop()
        if path.name != "manifest.json" or path.parent.name != "campaign":
            raise SimulationCampaignIntegrityError("expected exact Campaign manifest path")
        store = CampaignStore(path.parent)
        manifest = store.load_manifest()
        if expected is not None and manifest_digest(manifest) != expected:
            raise SimulationCampaignIntegrityError("prerequisite hook manifest digest disagrees")
        campaign_id = str(manifest.document["campaign_id"])
        if campaign_id in ancestors:
            raise SimulationCampaignIntegrityError("cyclic prerequisite hook evidence")
        if path in visited:
            continue
        visited.add(path)
        for firing in store.inspect_pre_sim_firings(manifest):
            firings[firing.key] = firing
        for entry in manifest.document["prerequisites"]:
            reference = entry["manifest"]
            edge = (store.root.parents[2] / reference["path"], reference["sha256"])
            chain = ancestors | {campaign_id}
            if (*edge, chain) not in queued:
                queued.add((*edge, chain))
                pending.append((*edge, chain))
    return tuple(firings.values())


def reread_pre_sim_firings(prior: tuple[PreSimFiring, ...]) -> tuple[PreSimFiring, ...]:
    """Freshly reread earlier-authenticated firings, one snapshot per attempt.

    No terminal record is consulted, so unrelated terminal corruption cannot
    drop valid firing evidence, and changed sidecar bytes change the reference
    and raise.  Only fresh firings matching a prior key are returned.
    """
    by_manifest: dict[Path, dict[Path, list[PreSimFiring]]] = {}
    for firing in prior:
        attempts = by_manifest.setdefault(firing.manifest_path, {})
        attempts.setdefault(firing.path.parent.parent, []).append(firing)
    fresh = {}
    for manifest_path, attempts in by_manifest.items():
        store = CampaignStore(manifest_path.parent)
        manifest = store.load_manifest()
        items = {item["work_item_id"]: item for item in manifest.document["work_items"]}
        for directory, owned in attempts.items():
            fresh.update(store._reread_prior_attempt(manifest, items, directory, owned))
    return tuple(fresh[key] for key in dict.fromkeys(firing.key for firing in prior))


def _aggregate_grade(grades: tuple[str, ...], *, complete: bool) -> str:
    if "error" in grades:
        return "error"
    if "fail" in grades:
        return "fail"
    if not complete or "inconclusive" in grades:
        return "inconclusive"
    return "pass"


def _validate_result_test_runs(
    work_item: Mapping[str, object], observations: tuple[Mapping[str, object], ...]
) -> None:
    """A result that names Test Runs must name the ones its work item recorded."""
    named = ["test_run" in observation for observation in observations]
    if not any(named):
        return
    bound = observation_test_runs(work_item, observations)
    if bound is None or [observation["test_run"] for observation in observations] != [
        name for name, _fingerprint in bound
    ]:
        raise SimulationCampaignIntegrityError(
            "result observations name Test Runs their work item does not record"
        )


# Result states in which an unfiltered Cocotb batch may report no Test at all.
_UNNAMED_FAILURE_STATES = frozenset(
    {"blocked_by_build", "setup_error", "timeout", "crash", "aborted"}
)


def _expected_observed_tests(
    work_item: Mapping[str, object], names: tuple[str, ...]
) -> tuple[object, ...]:
    """The Test of each observation a named work item must report, in order.

    One observation per recorded Test Run; a manifest before v4 names Tests
    only, one observation each.
    """
    recorded = cast("tuple[Mapping[str, object], ...] | None", work_item.get("test_runs"))
    return names if recorded is None else tuple(run["test"] for run in recorded)


def _validate_result_selection(
    work_item: Mapping[str, object],
    result: SimulationResult,
    attempt_directory: Path,
    *,
    target_identity: str | None = None,
) -> None:
    """Bind terminal observation order and transport evidence to the manifest."""
    selection = cast(Mapping[str, object], work_item["selection"])
    names = cast(tuple[str, ...], selection["names"])
    observations = cast(tuple[Mapping[str, object], ...], result.document["observations"])
    observed = tuple(item["test"] for item in observations)
    kind = cast(str, work_item["kind"])
    if selection["kind"] == "named" and observed != _expected_observed_tests(work_item, names):
        raise SimulationCampaignIntegrityError(
            "result observations disagree with named work-item selection"
        )
    _validate_result_test_runs(work_item, observations)
    if selection["kind"] == "unfiltered":
        unnamed_failure = (
            observed == (None,) and result.document["state"] in _UNNAMED_FAILURE_STATES
        )
        discovered = bool(observed) and all(isinstance(name, str) and name for name in observed)
        if not (unnamed_failure or discovered):
            raise SimulationCampaignIntegrityError(
                "unfiltered Cocotb result has invalid observation identity"
            )
    if result.document["state"] not in {"completed", "timeout", "crash", "aborted"}:
        return
    evidence = cast(tuple[Mapping[str, object], ...], result.document["evidence"])
    expected_kind = "coverage_campaign_manifest" if kind == "coverage_aggregate" else None
    if kind == "cocotb_batch" and observed != (None,):
        expected_kind = "cocotb_results"
    if (
        kind == "cocotb_batch"
        and observed == (None,)
        and result.document["state"] == "aborted"
        and observations[0]["failure_class"] == "infrastructure"
    ):
        infrastructure_tests = _authenticated_infrastructure_tests(
            attempt_directory, result, target_identity
        )
        if not _missing_transport_is_infrastructure(result, infrastructure_tests):
            raise SimulationCampaignIntegrityError(
                "unfiltered infrastructure result lacks authenticated failure evidence"
            )
    if expected_kind is not None:
        infrastructure_tests = _authenticated_infrastructure_tests(
            attempt_directory, result, target_identity
        )
        matching = [item for item in evidence if item["kind"] == expected_kind]
        if not matching and _missing_transport_is_infrastructure(result, infrastructure_tests):
            return
        if len(matching) != 1 or matching[0]["owner"] != result.document["attempt_id"]:
            raise SimulationCampaignIntegrityError(
                f"{kind} result lacks exact authenticated transport evidence"
            )
        if kind == "cocotb_batch":
            _validate_cocotb_transport(
                attempt_directory, matching[0], observations, infrastructure_tests
            )


def _missing_transport_is_infrastructure(
    result: SimulationResult, infrastructure_tests: frozenset[object]
) -> bool:
    document = result.document
    observations = cast(tuple[Mapping[str, object], ...], document["observations"])
    build = cast(Mapping[str, object], document["build_result"])
    return document["state"] == "aborted" and (
        build["state"] == "infrastructure_error"
        or all(
            item["test"] in infrastructure_tests
            and item["failure_class"] == "infrastructure"
            and item["execution"] == "aborted"
            for item in observations
        )
    )


def _authenticated_infrastructure_tests(
    directory: Path, result: SimulationResult, target_identity: str | None
) -> frozenset[object]:
    document = result.document
    references = cast(tuple[Mapping[str, object], ...], document["evidence"])
    observations = cast(tuple[Mapping[str, object], ...], document["observations"])
    names = tuple(item["test"] for item in observations)
    authenticated: set[object] = set()
    for reference in references:
        if reference["kind"] not in {
            "simulation_adapter_result",
            "simulation_infrastructure_failure",
        }:
            continue
        payload, path = _read_infrastructure_reference(
            directory, reference, document["attempt_id"]
        )
        if reference["kind"] == "simulation_infrastructure_failure":
            if (
                payload.get("$schema") != "booley.simulation-infrastructure-failure/v1"
                or payload.get("attempt_id") != document["attempt_id"]
                or payload.get("work_item_id") != document["work_item_id"]
                or payload.get("selected_tests") != list(names)
                or payload.get("source") != "process"
                or not isinstance(payload.get("code"), str)
                or not payload["code"]
                or payload.get("termination") != payload["code"]
            ):
                raise SimulationCampaignIntegrityError("infrastructure process evidence disagrees")
            authenticated.update(names)
        else:
            authenticated.update(
                _adapter_infrastructure_tests(path, payload, observations, target_identity)
            )
    return frozenset(authenticated)


def _read_infrastructure_reference(directory, reference, attempt_id):
    path = directory / cast(str, reference["path"])
    _require_safe_parents(path, directory)
    raw = _read_regular(path, limit=RECORD_MAX_BYTES)
    if (
        reference["owner"] != attempt_id
        or len(raw) != reference["bytes"]
        or _raw_digest(raw) != reference["sha256"]
    ):
        raise SimulationCampaignIntegrityError("infrastructure evidence does not authenticate")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError("infrastructure evidence is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SimulationCampaignIntegrityError("infrastructure evidence is invalid")
    return payload, path


def _adapter_infrastructure_tests(path, payload, observations, target_identity) -> set[object]:
    if target_identity is not None and payload.get("target_identity") != target_identity:
        raise SimulationCampaignIntegrityError("infrastructure adapter Target disagrees")
    names = tuple(item["test"] for item in observations)
    try:
        identity = AdapterTransportIdentity(
            adapter=payload["adapter"],
            attempt_token=payload["attempt_token"],
            target_identity=payload["target_identity"],
            selected_tests=tuple(payload["selected_tests"]),
            result_path=path,
        )
        source = read_adapter_result(identity)
    except (KeyError, TypeError, AdapterTransportError) as exc:
        raise SimulationCampaignIntegrityError(
            "infrastructure adapter evidence is invalid"
        ) from exc
    if names == (None,) and not source.tests and not source.test_results:
        return _unfiltered_adapter_infrastructure_test(source, observations[0])
    if source.tests != names:
        raise SimulationCampaignIntegrityError("infrastructure adapter selection disagrees")
    authenticated: set[object] = set()
    for item, observed in zip(source.test_results, observations, strict=True):
        if item.failure_kind != "infrastructure" or item.termination == "completed":
            continue
        detail = cast(Mapping[str, object], observed["detail"])
        if (
            observed["execution"] != "aborted"
            or observed["failure_class"] != "infrastructure"
            or detail.get("termination") != item.termination
        ):
            raise SimulationCampaignIntegrityError("infrastructure adapter termination disagrees")
        authenticated.add(item.name)
    return authenticated


def _unfiltered_adapter_infrastructure_test(source, observed) -> set[object]:
    detail = cast(Mapping[str, object], observed["detail"])
    if (
        source.failure_kind == "infrastructure"
        and source.termination != "completed"
        and observed["execution"] == "aborted"
        and observed["failure_class"] == "infrastructure"
        and detail.get("termination") == source.termination
    ):
        return {None}
    raise SimulationCampaignIntegrityError("unfiltered adapter failure disagrees")


def _validate_cocotb_transport(
    attempt_directory: Path,
    reference: Mapping[str, object],
    observations: tuple[Mapping[str, object], ...],
    infrastructure_tests: frozenset[object] = frozenset(),
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
        # The transport records what the adapter observed; the Test Run name
        # is added by the campaign from the work item and authenticated there.
        or transported
        != [
            {key: value for key, value in item.items() if key != "test_run"}
            for item in observations
        ]
    ):
        raise SimulationCampaignIntegrityError(
            "Cocotb transport observations disagree with Simulation Result"
        )
    _validate_cocotb_source(
        document["source_observations"], observations, infrastructure_tests=infrastructure_tests
    )


def _validate_cocotb_source(
    source: object,
    observations: tuple[Mapping[str, object], ...],
    *,
    infrastructure_tests: frozenset[object] = frozenset(),
) -> None:
    if not isinstance(source, list) or len(source) != len(observations):
        raise SimulationCampaignIntegrityError("Cocotb source transport count disagrees")
    for transported, observed in zip(source, observations, strict=True):
        if not isinstance(transported, dict) or set(transported) != {"test", "verdict", "detail"}:
            raise SimulationCampaignIntegrityError("Cocotb source transport is invalid")
        verdict_matches = _cocotb_source_verdict_matches(
            transported, observed, infrastructure=observed["test"] in infrastructure_tests
        )
        detail_matches = _cocotb_source_detail_matches(
            transported,
            observed,
            infrastructure=observed["test"] in infrastructure_tests,
            limit=16 if len(observations) > 128 else 768,
        )
        if transported["test"] != observed["test"] or not verdict_matches or not detail_matches:
            raise SimulationCampaignIntegrityError("Cocotb source transport disagrees")


def _cocotb_source_detail_matches(
    transported: Mapping[str, object],
    observed: Mapping[str, object],
    *,
    infrastructure: bool,
    limit: int,
) -> bool:
    source = transported["detail"]
    reason = cast(Mapping[str, object], observed["detail"])["reason"]
    if not source or source == reason:
        return True
    if not (
        infrastructure
        and observed["execution"] == "aborted"
        and observed["failure_class"] == "infrastructure"
        and isinstance(source, str)
    ):
        return False
    return source.encode("utf-8")[-limit:].decode("utf-8", errors="ignore") == reason


def _cocotb_source_verdict_matches(
    transported: Mapping[str, object],
    observed: Mapping[str, object],
    *,
    infrastructure: bool = False,
) -> bool:
    verdict = transported["verdict"]
    functional = observed["functional"]
    if (
        infrastructure
        and observed["execution"] == "aborted"
        and observed["failure_class"] == "infrastructure"
        and functional == "not_observed"
    ):
        return verdict in {"fail", "timeout", "inconclusive"}
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
        not (build["ran"] is True or _is_recorded_reuse_hit(build))
        or build["verdict"] != "pass"
        or build["failure_kind"] is not None
        or build["returncode"] != 0
        or build["timed_out"] is not False
        or any(
            not isinstance(build[field], str) for field in ("output", "reason", "cache_decision")
        )
    ):
        raise SimulationCampaignIntegrityError("normalized build evidence is not successful")


def _is_recorded_reuse_hit(build: Mapping[str, object]) -> bool:
    """Whether a build record says a retained image served it (``ran`` false, ``hit``).

    The store checks only the recorded decision; the engine verified the image.
    """
    decision = build["cache_decision"]
    return (
        build["ran"] is False
        and isinstance(decision, str)
        and decision.startswith(f"{CacheDecision.HIT};")
    )


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


__all__ = ["CampaignRecovery", "CampaignStore", "SharedBuildRecovery", "WorkItemRecovery"]
