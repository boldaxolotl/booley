"""Crash-safe storage for one durable Simulation Campaign.

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
from types import MappingProxyType
from typing import TypeVar, cast

from booley.core.booley_dir_links import is_booley_dir_entry
from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_finite_number,
    require_int,
    require_list,
    require_str_value,
)
from booley.flows.artifact_durability import (
    durable_create,
    durable_directory,
    fsync_directory,
)
from booley.flows.sim.build_session import CacheDecision
from booley.flows.sim.campaign_files import (
    parse_json_object,
    read_bounded,
    read_json_object,
    stream_digest,
)
from booley.flows.sim.campaign_reports import is_report_link
from booley.flows.sim.coverage_json import content_digest
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.file_lock import nonblocking_file_lock

from .bundle import require_bundle_inventory
from .codec import (
    MANIFEST_MAX_BYTES,
    RECORD_MAX_BYTES,
    SUMMARY_MAX_BYTES,
    SimulationCampaignIntegrityError,
    UnsupportedSimulationCampaignFormatError,
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
    linked_manifest_path,
    reads_campaign_records,
    validate_relative_path,
)
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    SimulationAttempt,
    SimulationCampaignDocument,
    SimulationCampaignManifest,
    SimulationResult,
    SimulatorBundle,
    StrictGrade,
)
from .planning import manifest_digest
from .pre_sim_evidence import PreSimFiring, PreSimFiringDecoder, _reference

_T = TypeVar("_T", bound=SimulationCampaignDocument)
_SUMMARY_SCHEMA = "booley.simulation-campaign-summary/v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COMMON_BUILD_FIELDS = (
    "campaign_id",
    "manifest_sha256",
    "workload_sha256",
    "build_variant_id",
)


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


def _invocation_of(campaign_root: Path) -> Path | None:
    """The invocation directory holding ``<invocation>/targets/<target>/campaign``, if laid out so."""
    parents = campaign_root.parents
    if len(parents) > 2 and parents[1].name == "targets":
        return parents[2]
    return None


def _require_safe_parents(path: Path, root: Path, booley_dir: Path | None = None) -> None:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"campaign path escapes store: {path}") from exc
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if is_report_link(current) and not is_booley_dir_entry(current, booley_dir):
            raise SimulationCampaignIntegrityError(f"campaign path contains a link: {current}")


def _create_immutable(path: Path, raw: bytes) -> None:
    durable_create(path, raw)


class CampaignStore:
    """Append-only authoritative storage rooted at one ``campaign`` directory."""

    def __init__(self, campaign_directory: Path, *, booley_dir: Path | None = None) -> None:
        self.root = campaign_directory.absolute()
        # The one link component the store may cross (#2191); ``None`` is strict.
        self.booley_dir = booley_dir
        self._authenticated_files: set[Path] = set()

    @property
    def retention_files(self) -> tuple[Path, ...]:
        """Files this store read or hashed, in stable order."""
        return tuple(sorted(self._authenticated_files))

    def _read(self, path: Path, *, limit: int) -> bytes:
        raw = read_bounded(
            path,
            limit=limit,
            error=SimulationCampaignIntegrityError,
            booley_dir=self.booley_dir,
            single_link=True,
        )
        self._authenticated_files.add(path.absolute())
        return raw

    def _identity(self, path: Path) -> tuple[int, str]:
        if is_report_link(path) or not path.is_file():
            raise SimulationCampaignIntegrityError(f"evidence is not a regular file: {path}")
        identity = stream_digest(path)
        self._authenticated_files.add(path.absolute())
        return identity

    def _decode_located(self, decode: Callable[[bytes], _T], path: Path, limit: int) -> _T:
        """Read and decode one record; a Booley 0.3.0 refusal names its invocation."""
        raw = self._read(path, limit=limit)
        try:
            return decode(raw)
        except UnsupportedSimulationCampaignFormatError as exc:
            invocation = _invocation_of(self.root)
            raise (exc if invocation is None else exc.located(invocation)) from None

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.json"

    def publish_manifest(self, manifest: SimulationCampaignManifest) -> str:
        raw = encode_simulation_campaign_manifest(manifest)
        _require_safe_parents(self.manifest_path, self.root, self.booley_dir)
        _create_immutable(self.manifest_path, raw)
        return manifest_digest(manifest)

    def load_manifest(self) -> SimulationCampaignManifest:
        _require_safe_parents(self.manifest_path, self.root, self.booley_dir)
        return self._decode_located(
            decode_simulation_campaign_manifest, self.manifest_path, MANIFEST_MAX_BYTES
        )

    def load_summary(self) -> Mapping[str, object]:
        """Load the bounded retention fields from the existing summary projection."""
        _require_safe_parents(self.summary_path, self.root, self.booley_dir)
        raw = self._read(self.summary_path, limit=SUMMARY_MAX_BYTES)
        document = parse_json_object(raw, error=SimulationCampaignIntegrityError)
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
        return manifest_digest(self.load_manifest())

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Fail immediately when another scheduler owns this campaign."""
        _require_safe_parents(self.root, self.root, self.booley_dir)
        if not self.root.is_dir():
            raise SimulationCampaignIntegrityError("Simulation Campaign directory disappeared")
        path = self.root / ".lock"
        _require_safe_parents(path, self.root, self.booley_dir)
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
        name = f"{ordinal:04d}-{digest}"
        # Decode does not check the id, and retention globs assume one component:
        # refuse a "/" here, and a backslash (a separator on Windows) below.
        if "/" in name:
            raise SimulationCampaignIntegrityError(
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

    @reads_campaign_records
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
        if document["state"] not in {"ready", "design_failure", "infrastructure_error"}:
            raise SimulationCampaignIntegrityError("shared Build Result state is invalid")
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
        execution = _load_build_execution(directory, document, self.booley_dir)
        return bundle, execution

    def _load_shared_bundle(self, directory, attempt, result, expected) -> SimulatorBundle:
        binding = cast(Mapping[str, object], result.document["bundle"])
        path = directory / validate_relative_path(binding["manifest_path"])
        raw = self._read(path, limit=RECORD_MAX_BYTES)
        if (
            len(raw) != binding["manifest_bytes"]
            or content_digest(raw) != binding["manifest_sha256"]
        ):
            raise SimulationCampaignIntegrityError("shared Build Result bundle digest disagrees")
        bundle = decode_simulator_bundle(raw)
        _validate_shared_bundle(bundle, binding, attempt, expected)
        require_bundle_inventory(bundle)
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
        _require_safe_parents(path, self.root, self.booley_dir)
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

    def _reread_prior_attempt(
        self,
        manifest: SimulationCampaignManifest,
        items: Mapping[str, Mapping[str, object]],
        directory: Path,
        owned: list[PreSimFiring],
    ) -> dict[tuple[str, str, str, int], PreSimFiring]:
        """Check one attempt once and return the fresh firings matching its prior ones."""
        digest = manifest_digest(manifest)
        item = items.get(owned[0].key[1])
        if any(firing.document["manifest_sha256"] != digest for firing in owned):
            raise SimulationCampaignIntegrityError("prior hook manifest changed")
        if item is None:
            raise SimulationCampaignIntegrityError("prior hook manifest owner changed")
        _require_safe_parents(directory, self.root, self.booley_dir)
        if directory.parent != self._work_item_directory(item) / "attempts":
            raise SimulationCampaignIntegrityError("prior hook attempt owner changed")
        fresh = {
            candidate.key: candidate
            for candidate in self._attempt_firings(manifest, item, directory, None)
        }
        for firing in owned:
            match = fresh.get(firing.key)
            if match is None or (match.path, match.reference) != (firing.path, firing.reference):
                raise SimulationCampaignIntegrityError("prior hook reference changed")
        return {firing.key: fresh[firing.key] for firing in owned}

    @reads_campaign_records
    def _attempt_firings(
        self,
        manifest: SimulationCampaignManifest,
        item: Mapping[str, object],
        directory: Path,
        terminal: SimulationResult | None,
    ) -> tuple[PreSimFiring, ...]:
        """Authenticate one attempt's sidecars; ``terminal`` is the result binding this attempt.

        An attempt without sidecars and without a terminal result costs no read.
        A terminal attempt always has its result references compared with its sidecars.
        """
        if not (directory / "attempt.json").exists():
            return ()
        paths = self._pre_sim_paths(directory)
        if not paths and terminal is None:
            return ()
        attempt = self.read_attempt(directory)
        _require_attempt_directory_name(directory, attempt)
        firings: list[PreSimFiring] = []
        if paths:
            decoder = PreSimFiringDecoder(manifest)
            for ordinal, path in enumerate(paths, start=1):
                _require_safe_parents(path, directory, self.booley_dir)
                raw = self._read(path, limit=MANIFEST_MAX_BYTES)
                firing = PreSimFiring(
                    self.manifest_path,
                    path,
                    MappingProxyType(_reference(path, raw, directory.name.split("-", 1)[1])),
                    decoder.decode(raw, item, attempt),
                    terminal is not None,
                )
                if firing.document["ordinal"] != ordinal or path.name != f"{ordinal:04d}.json":
                    raise SimulationCampaignIntegrityError("Pre-Sim Commands ordinal disagrees")
                firings.append(firing)
        if terminal is not None:
            if [
                reference
                for reference in terminal.document["evidence"]
                if reference["kind"] == "pre_sim_commands"
            ] != [firing.reference for firing in firings]:
                raise SimulationCampaignIntegrityError(
                    "Pre-Sim Commands result references disagree"
                )
            if firings:
                _validate_result_attempt_binding(attempt, terminal)
        return tuple(firings)

    def _campaign_firings(
        self, manifest: SimulationCampaignManifest, recovery: CampaignRecovery
    ) -> tuple[PreSimFiring, ...]:
        """Authenticate every retained attempt's firings against the scan's terminal results."""
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        firings: list[PreSimFiring] = []
        for item, recovered in zip(items, recovery.items, strict=True):
            directories = self._attempt_directories(self._work_item_directory(item) / "attempts")
            for directory in directories:
                # The scan proved a result binds the final attempt, so only it is terminal.
                terminal = recovered.result if directory == directories[-1] else None
                firings.extend(self._attempt_firings(manifest, item, directory, terminal))
        return tuple(firings)

    def _authenticate_reference(self, root: Path, reference: Mapping[str, object]) -> None:
        path = root / validate_relative_path(reference["path"])
        _require_safe_parents(path, root, self.booley_dir)
        size, digest = self._identity(path)
        if size != reference["bytes"] or digest != reference["sha256"]:
            raise SimulationCampaignIntegrityError(
                f"result evidence reference does not authenticate {path}"
            )

    def _publish(self, path: Path, value: _T, encoder: Callable[[_T], bytes]) -> Path:
        _require_safe_parents(path, self.root, self.booley_dir)
        _create_immutable(path, encoder(value))
        return path

    def scan(
        self,
        manifest: SimulationCampaignManifest | None = None,
        manifest_sha256: str | None = None,
    ) -> CampaignRecovery:
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
                raise SimulationCampaignIntegrityError(
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
        if is_report_link(root) or not root.is_dir():
            raise SimulationCampaignIntegrityError(f"invalid attempts directory: {root}")
        children = sorted(root.iterdir(), key=lambda item: item.name)
        for ordinal, child in enumerate(children, start=1):
            prefix = f"{ordinal:04d}-"
            if not child.name.startswith(prefix) or is_report_link(child) or not child.is_dir():
                raise SimulationCampaignIntegrityError(f"invalid attempt directory: {child}")
        return tuple(children)

    def regenerate_summary(
        self,
        manifest: SimulationCampaignManifest | None = None,
        manifest_sha256: str | None = None,
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
            raise SimulationCampaignIntegrityError("Simulation Result grade is invalid")
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
        _require_safe_parents(self.summary_path, self.root, self.booley_dir)
        atomic_replace_bytes(self.summary_path, raw)
        return json.loads(raw)


_PRE_SIM_DAG_BOUND = 4096


def collect_pre_sim_firings(
    *manifest_paths: Path,
    booley_dir: Path | None = None,
    prior: tuple[PreSimFiring, ...] = (),
) -> tuple[PreSimFiring, ...]:
    """Read Pre-Sim firings; the only entry that does, in one of two modes.

    With ``manifest_paths``, read every root's prerequisite DAG once and
    deduplicate by full firing identity.  The key ``(campaign_id, work_item_id,
    attempt_id, ordinal)`` keeps Candidate and Cycle Count baseline firings
    distinct.  Each distinct manifest path is scanned once.  Prerequisite
    references repeated by many work items count once per ancestor chain toward
    the bound.

    With ``prior`` (and no paths), freshly reread earlier-authenticated firings,
    one read per attempt.  No scan runs and no terminal record is consulted, so
    unrelated terminal corruption cannot drop valid firing evidence, and changed
    sidecar bytes change the reference and raise.  Only fresh firings matching a
    prior key are returned.
    """
    if prior:
        return _reread_prior(prior, booley_dir)
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
        store = CampaignStore(path.parent, booley_dir=booley_dir)
        manifest = store.load_manifest()
        digest = manifest_digest(manifest)
        if expected is not None and digest != expected:
            raise SimulationCampaignIntegrityError("prerequisite hook manifest digest disagrees")
        campaign_id = str(manifest.document["campaign_id"])
        if campaign_id in ancestors:
            raise SimulationCampaignIntegrityError("cyclic prerequisite hook evidence")
        if path in visited:
            continue
        visited.add(path)
        for firing in store._campaign_firings(manifest, store.scan(manifest, digest)):
            firings[firing.key] = firing
        for entry in manifest.document["prerequisites"]:
            reference = entry["manifest"]
            edge = (linked_manifest_path(store.root.parents[2], reference), reference["sha256"])
            chain = ancestors | {campaign_id}
            if (*edge, chain) not in queued:
                queued.add((*edge, chain))
                pending.append((*edge, chain))
    return tuple(firings.values())


def _reread_prior(
    prior: tuple[PreSimFiring, ...], booley_dir: Path | None
) -> tuple[PreSimFiring, ...]:
    """Reread each attempt owning a prior firing once; raise when any reference changed."""
    by_manifest: dict[Path, dict[Path, list[PreSimFiring]]] = {}
    for firing in prior:
        attempts = by_manifest.setdefault(firing.manifest_path, {})
        attempts.setdefault(firing.path.parent.parent, []).append(firing)
    fresh = {}
    for manifest_path, attempts in by_manifest.items():
        store = CampaignStore(manifest_path.parent, booley_dir=booley_dir)
        manifest = store.load_manifest()
        items = {item["work_item_id"]: item for item in manifest.document["work_items"]}
        for directory, owned in attempts.items():
            fresh.update(store._reread_prior_attempt(manifest, items, directory, owned))
    return tuple(fresh[key] for key in dict.fromkeys(firing.key for firing in prior))


def _require_attempt_directory_name(directory: Path, attempt: SimulationAttempt) -> None:
    """Refuse an attempt stored under a directory its own ordinal and id do not name."""
    document = attempt.document
    if directory.name != f"{document['attempt_ordinal']:04d}-{document['attempt_id']}":
        raise SimulationCampaignIntegrityError("hook attempt directory owner disagrees")


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


def _load_build_execution(
    directory: Path, result: Mapping[str, object], booley_dir: Path | None = None
) -> Mapping[str, object]:
    references = cast(tuple[Mapping[str, object], ...], result["evidence"])
    matches = [item for item in references if item["kind"] == "simulation_build_execution"]
    if len(matches) != 1:
        raise SimulationCampaignIntegrityError("ready shared build lacks exact execution evidence")
    document = read_json_object(
        directory / cast(str, matches[0]["path"]),
        limit=RECORD_MAX_BYTES,
        error=SimulationCampaignIntegrityError,
        booley_dir=booley_dir,
        single_link=True,
        canonical=True,
    )
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
