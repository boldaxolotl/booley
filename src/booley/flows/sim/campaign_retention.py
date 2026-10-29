"""Exact Simulation Campaign retention operations; normalized evidence is immutable.

Native removal writes a ``pruning`` availability sidecar before deleting ``native/``,
so an interrupted cleanup is explicit and retryable.
Full removal leaves an empty numbered tombstone to prevent invocation-id reuse.
"""

import json
import shutil
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.core.booley_layout import DOCTOR_TMP_DIR, GOALS_DIR_NAME, INTERACTIVE_LOGS_DIR_NAME
from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_list,
    require_str_value,
    require_uuid4,
)
from booley.flows.progress_lifecycle import validate_progress_shape
from booley.runtime.file_lock import LockContentionError
from booley.runtime.filesystem_utils import is_transient_read_error, read_file_bytes
from booley.runtime.sandbox_layout import canonical_project_alias_path
from booley.runtime.session_paths import RUNTIME_DIR

from .campaign import (
    ProjectionTrust,
    RetainedCampaignStatus,
    SimulationCampaignIntegrityError,
    SimulationProjection,
    UnsupportedSimulationCampaignFormatError,
    authenticate_retained_campaign_inventory,
    decode_simulation_projection,
    inspect_retained_campaign,
    recover_retained_campaign_resources,
    resolve_artifact_reference,
    retained_campaign_lock,
)
from .campaign.artifact_reference import (
    ArtifactReferenceError,
    decode_artifact_reference,
    resolve_report_artifact_reference,
)
from .campaign.codec import MANIFEST_MAX_BYTES
from .campaign.dependency import (
    CampaignDependencyReceipt,
    dependency_receipt_files,
    read_campaign_dependencies,
)
from .campaign_reports import (
    campaign_invocation_lock,
    is_report_link,
    report_link_ancestor,
    simulation_booley_dir,
    target_report_directory,
    write_campaign_json,
)
from .coverage_campaign import CoverageCampaign
from .coverage_campaign_store import LoadedCoverageCampaign, load_coverage_campaign
from .coverage_reference import (
    MAX_REFERENCE_BYTES,
    REFERENCE_SCHEMA,
    authenticate_coverage_campaign_owner,
    resolve_coverage_campaign_reference,
)


class CampaignRetentionError(ValueError):
    """A selection or filesystem tree cannot be safely pruned."""


class _UnreadableDependencyError(CampaignRetentionError):
    """A dependency scan hit a lasting sharing error and must not guess."""


# Windows file locks are mandatory byte-range locks: while any handle (including
# one held by this very prune) locks the sentinel byte, other handles cannot read it.
_MANDATORY_FILE_LOCKS = sys.platform == "win32"
_BATCH_SCHEMA = "booley.simulation-prune-batch/v1"


@dataclass(frozen=True, slots=True)
class _CampaignEligibility:
    status: RetainedCampaignStatus
    abandoned: bool


@dataclass(frozen=True, slots=True)
class _DependentInvocation:
    path: Path
    receipt: CampaignDependencyReceipt | None
    authenticated: bool


@dataclass(frozen=True, slots=True)
class _PruneBatch:
    path: Path
    members: tuple[Path, ...]


@contextmanager
def _retention_invocation_lock(root: Path) -> Iterator[None]:
    with ExitStack() as stack:
        try:
            stack.enter_context(
                campaign_invocation_lock(root, booley_dir=invocation_booley_dir(root))
            )
        except LockContentionError as exc:
            raise CampaignRetentionError(
                f"invocation {root.name} is still being produced; retry after it finishes"
            ) from exc
        yield


def _remove_tree(path: Path) -> None:
    """Remove an authenticated artifact tree, including read-only snapshots."""
    for child in path.rglob("*"):
        if child.is_file():
            child.chmod(child.stat().st_mode | stat.S_IWUSR)
    path.chmod(path.stat().st_mode | stat.S_IWUSR)
    shutil.rmtree(path)


# Booley Directory children whose ``<id>/.runtime`` holds Flow reports for agent runs:
# Interactive Mode MCP sessions and Goal Mode Goal Records.
_RUNTIME_OWNER_DIRS = frozenset({INTERACTIVE_LOGS_DIR_NAME, GOALS_DIR_NAME})


def invocation_booley_dir(root: Path) -> Path | None:
    """Return the Booley Directory entry an invocation root lives under, if it is one.

    *root* is ``<reports_root>/sim/<N>`` (or a sibling such as its quarantine). The
    reports root is ``<entry>/flow-reports``, ``<entry>/.runtime/flow-reports``, deep
    Doctor's ``<entry>/tmp/doctor/flow-reports``, or a per-run runtime root
    ``<entry>/<owner>/<id>/.runtime/flow-reports`` (see :data:`_RUNTIME_OWNER_DIRS`);
    the candidate counts only when it is exactly the configured Booley Directory of the
    checkout containing it (#2191). Custom report roots get ``None`` and stay strict.
    """
    reports_root = root.parent.parent
    if reports_root.name != "flow-reports":
        return None
    candidate = reports_root.parent
    if _ends_with(candidate, DOCTOR_TMP_DIR):
        candidate = candidate.parents[len(DOCTOR_TMP_DIR.parts) - 1]
    elif candidate.name == RUNTIME_DIR:
        candidate = candidate.parent
        # Per-run runtime roots: strip exactly one ``<owner>/<id>`` pair.
        if candidate.parent.name in _RUNTIME_OWNER_DIRS:
            candidate = candidate.parent.parent
    if simulation_booley_dir(candidate.parent) != candidate:
        return None
    return candidate


def _ends_with(path: Path, suffix: Path) -> bool:
    """Whether *path*'s last components are exactly *suffix*'s (lexical only)."""
    return path.parts[-len(suffix.parts) :] == suffix.parts


def _safe_tree(path: Path, booley_dir: Path | None) -> None:
    parent = report_link_ancestor(path, booley_dir)
    if parent is not None:
        raise CampaignRetentionError(f"Symlinks are not allowed: {parent}")
    if path.is_dir():
        for child in path.rglob("*"):
            if is_report_link(child) or not (child.is_file() or child.is_dir()):
                raise CampaignRetentionError(f"Unsafe artifact: {child}")


def _canonical_retention_path(path: Path) -> Path:
    try:
        return canonical_project_alias_path(Path(path).absolute())
    except ValueError as exc:
        raise CampaignRetentionError(str(exc)) from exc


def _invocation(reports_root: Path, invocation: int) -> Path:
    if type(invocation) is not int or invocation < 1:
        raise CampaignRetentionError("Select one exact positive invocation number")
    root = _canonical_retention_path(reports_root) / "sim" / str(invocation)
    _safe_tree(root, invocation_booley_dir(root))
    return root


def _read_object(path: Path) -> dict[str, object]:
    document = json.loads(read_file_bytes(path).decode("utf-8"))
    if not isinstance(document, dict):
        raise CampaignRetentionError(f"Expected an object: {path}")
    return document


def _fail_closed_on_sharing_error(error: OSError, path: Path) -> None:
    """Refuse a destructive decision resting on a file a sharing error kept unreadable.

    Dependency scans answer "not a dependent" for an unreadable file; a Windows
    sharing error that outlasted the retry budget is not evidence of absence.
    """
    if is_transient_read_error(error):
        raise _UnreadableDependencyError(f"cannot read {path}: {error}") from error


def _target(
    root: Path, selector: str, *, require_projection: bool = True
) -> tuple[Path, LoadedCoverageCampaign]:
    if not isinstance(selector, str) or not selector or selector in {".", "..", "latest"}:
        raise CampaignRetentionError("Select one exact Target selector")
    progress = _read_object(root / "progress.json")
    targets = progress.get("targets")
    if (
        progress.get("flow") != "sim"
        or not isinstance(targets, list)
        or targets.count(selector) != 1
    ):
        raise CampaignRetentionError("Target does not resolve exactly in this invocation")
    target = target_report_directory(root, selector)
    public_path = target / "coverage.json"
    public = _read_object(public_path)
    if public.get("$schema") == REFERENCE_SCHEMA:
        resolved = resolve_coverage_campaign_reference(public_path)
        authenticate_coverage_campaign_owner(resolved)
        loaded = resolved.loaded
        storage = resolved.campaign_path.parent
    else:
        loaded = load_coverage_campaign(public_path)
        storage = target
    campaign = loaded.campaign
    if campaign.target.selector != selector or campaign.invocation["id"] != int(root.name):
        raise CampaignRetentionError("Campaign identity disagrees with the exact selection")
    if not require_projection:
        return storage, loaded
    projection = _read_object(target / "simulation.json")
    if (
        projection.get("target_identity") != campaign.target.identity
        or projection.get("complete") is not True
    ):
        raise CampaignRetentionError(
            "Simulation projection is missing or belongs to another Target"
        )
    return storage, loaded


def _native_artifacts(campaign: CoverageCampaign) -> list[dict[str, object]]:
    artifacts = []
    for artifact in campaign.artifacts:
        if artifact.kind not in {"raw_native", "merged_native"}:
            continue
        path = Path(artifact.path)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "native":
            raise CampaignRetentionError("Native artifact is outside the Target native directory")
        artifacts.append({"id": artifact.id, "path": artifact.path, "sha256": artifact.sha256})
    return artifacts


def prune_native_payload(reports_root: Path, invocation: int, target: str) -> Path:
    """Remove one exact Target's `native/` directory; return its availability sidecar."""
    root = _invocation(reports_root, invocation)
    with _retention_invocation_lock(root):
        return _prune_native(root, target)


def _prune_native(root: Path, target: str) -> Path:
    _safe_tree(root, invocation_booley_dir(root))
    _validate_native_pruning_eligibility(root, target)
    directory, loaded = _target(root, target)
    sidecar = directory / "availability.json"
    document = _availability_document(loaded, status="pruning")
    if not sidecar.exists():
        # First attempt: a recorded native payload must still be present. A retry
        # trusts the sidecar Booley wrote and only repeats the removal.
        for artifact in loaded.campaign.artifacts:
            if (
                artifact.kind in {"raw_native", "merged_native"}
                and artifact.sha256
                and not (directory / artifact.path).is_file()
            ):
                raise CampaignRetentionError(
                    f"Native payload is unexpectedly missing: {artifact.path}"
                )
        write_campaign_json(sidecar, document)
    native = directory / "native"
    if native.exists():
        _remove_tree(native)
    write_campaign_json(sidecar, {**document, "status": "pruned"})
    return sidecar


def prune_invocation(
    reports_root: Path,
    invocation: int,
    *,
    project_data: Path | None = None,
    include_dependents: bool = False,
) -> None:
    """Remove one invocation, optionally including authenticated resume dependents."""
    if project_data is not None:
        project_data = _canonical_retention_path(project_data)
    root = _invocation(reports_root, invocation)
    with _retention_invocation_lock(root):
        if _completed_tombstone(root):
            return
        batch = _read_prune_batch(root)
        if batch is not None:
            if not include_dependents:
                raise CampaignRetentionError(
                    "An interrupted dependent prune must resume with --include-dependents"
                )
            dependencies = _journal_dependents(root, batch.members)
            _prune_invocation_batch(root, dependencies, project_data=project_data)
            return
        dependencies = _dependent_invocations(root)
        live = tuple(item for item in dependencies if not _completed_tombstone(item.path))
        if live and not include_dependents:
            names = ", ".join(f"{item.path} (invocation {item.path.name})" for item in live)
            raise CampaignRetentionError(f"Invocation has dependent resumes: {names}")
        if not include_dependents:
            _prune_invocation(root, project_data=project_data)
            return
        _prune_invocation_batch(root, dependencies, project_data=project_data)


def _prune_invocation_batch(
    root: Path,
    dependencies: tuple[_DependentInvocation, ...],
    *,
    project_data: Path | None,
) -> None:
    members = tuple(sorted((item.path for item in dependencies), key=str))
    journal_path = root / ".prune-batch.json"
    document = {
        "$schema": _BATCH_SCHEMA,
        "operation": "full-with-dependents",
        "origin": str(root.absolute()),
        "members": [str(path.absolute()) for path in members],
    }
    by_path = {item.path: item for item in dependencies}
    with ExitStack() as stack:
        for member in members:
            if _completed_tombstone(member):
                continue
            item = by_path[member]
            if not item.authenticated and not _recoverable_quarantine(member):
                raise CampaignRetentionError(
                    "Dependent report does not authenticate to the origin "
                    f"Simulation Campaign: {member}"
                )
            stack.enter_context(_retention_invocation_lock(member))
        _verify_locked_members(root, dependencies)
        for member in (*members, root):
            if not _completed_tombstone(member):
                _preflight_invocation(member, project_data=project_data)
        if _read_prune_batch(root) is None:
            if not root.is_dir():
                raise CampaignRetentionError("Pruning batch journal is missing during recovery")
            write_campaign_json(journal_path, document)
        for member in members:
            _prune_invocation(member, project_data=project_data)
        remaining = _remaining_dependencies(root, members)
        if remaining:
            raise CampaignRetentionError(
                f"Unselected Simulation Campaign dependency remains: {remaining[0]}"
            )
        _prune_invocation(root, project_data=project_data)


def _verify_locked_members(root: Path, dependencies: tuple[_DependentInvocation, ...]) -> None:
    expected = tuple(item.path for item in dependencies)
    if _read_prune_batch(root) is not None:
        refreshed = _journal_dependents(root, expected)
    else:
        refreshed = _dependent_invocations(root)
    if tuple(item.path for item in refreshed) != expected:
        raise CampaignRetentionError("Simulation Campaign dependencies changed during pruning")
    for item in refreshed:
        if (
            not item.authenticated
            and not _completed_tombstone(item.path)
            and not _recoverable_quarantine(item.path)
        ):
            raise CampaignRetentionError(
                f"Dependent report no longer authenticates to the origin: {item.path}"
            )


def _remaining_dependencies(root: Path, members: tuple[Path, ...]) -> tuple[Path, ...]:
    if all(_completed_tombstone(member) for member in members):
        return ()
    if not root.is_dir():
        return tuple(member for member in members if not _completed_tombstone(member))
    return tuple(
        item.path for item in _dependent_invocations(root) if not _completed_tombstone(item.path)
    )


def _preflight_invocation(root: Path, *, project_data: Path | None) -> None:
    booley_dir = invocation_booley_dir(root)
    _safe_tree(root, booley_dir)
    quarantine = root.with_name(f".pruned-{root.name}")
    _safe_tree(quarantine, booley_dir)
    if _completed_tombstone(root):
        return
    if _recoverable_quarantine(root):
        return
    if not root.is_dir():
        raise CampaignRetentionError(f"Dependent invocation does not exist: {root}")
    if _matching_prune_journal(root):
        return
    if not _empty_abandoned_reservation(root):
        _validate_invocation_targets(root)
        _validate_invocation_inventory(root)
        try:
            with _campaign_mutation_locks(_campaign_directories(root)):
                _preflight_campaign_resources(root, project_data)
        except CampaignRetentionError:
            raise
        except (OSError, ValueError, SimulationCampaignIntegrityError) as exc:
            raise CampaignRetentionError(
                f"Simulation Campaign resource preflight failed: {exc}"
            ) from exc


def _completed_tombstone(root: Path) -> bool:
    quarantine = root.with_name(f".pruned-{root.name}")
    return not root.exists() and quarantine.is_dir() and not any(quarantine.iterdir())


def _recoverable_quarantine(root: Path) -> bool:
    quarantine = root.with_name(f".pruned-{root.name}")
    return not root.exists() and (quarantine / ".prune.json").is_file()


def _read_prune_batch(root: Path) -> _PruneBatch | None:
    quarantine = root.with_name(f".pruned-{root.name}")
    candidates = tuple(
        path
        for path in (root / ".prune-batch.json", quarantine / ".prune-batch.json")
        if path.exists()
    )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise CampaignRetentionError("Pruning batch journal state is ambiguous")
    path = candidates[0]
    document = _read_object(path)
    if set(document) != {"$schema", "operation", "origin", "members"}:
        raise CampaignRetentionError("Pruning batch journal fields are invalid")
    if (
        document["$schema"] != _BATCH_SCHEMA
        or document["operation"] != "full-with-dependents"
        or document["origin"] != str(root.absolute())
    ):
        raise CampaignRetentionError("Pruning batch journal is invalid")
    try:
        raw_members = require_list(document["members"], field="pruning batch members")
    except BoundaryError as exc:
        raise CampaignRetentionError("Pruning batch journal is invalid") from exc
    members: list[Path] = []
    for value in raw_members:
        try:
            member = Path(require_str_value(value, field="pruning batch member"))
        except BoundaryError as exc:
            raise CampaignRetentionError("Pruning batch member is invalid") from exc
        if (
            not member.is_absolute()
            or member != member.absolute()
            or not member.name.isdecimal()
            or int(member.name) < 1
        ):
            raise CampaignRetentionError("Pruning batch member is not canonical")
        members.append(member)
    ordered = tuple(sorted(members, key=str))
    if tuple(members) != ordered or len(set(ordered)) != len(ordered):
        raise CampaignRetentionError("Pruning batch membership is invalid")
    return _PruneBatch(path, ordered)


def _journal_dependents(root: Path, members: tuple[Path, ...]) -> tuple[_DependentInvocation, ...]:
    current = (
        {item.path: item for item in _dependent_invocations(root, reject_unknown=False)}
        if root.is_dir()
        else {}
    )
    dependencies: list[_DependentInvocation] = []
    for member in members:
        item = current.get(member)
        if item is not None:
            dependencies.append(item)
        else:
            complete = _completed_tombstone(member) or _recoverable_quarantine(member)
            dependencies.append(_DependentInvocation(member, None, complete))
    return tuple(dependencies)


def _dependent_invocations(
    root: Path, *, reject_unknown: bool = True
) -> tuple[_DependentInvocation, ...]:
    found: dict[Path, _DependentInvocation] = {}
    manifests = tuple(sorted(root.glob("targets/*/campaign/manifest.json")))
    for manifest in manifests:
        try:
            receipts = read_campaign_dependencies(manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CampaignRetentionError(
                f"Simulation Campaign dependency registry is invalid: {exc}"
            ) from exc
        for receipt in receipts:
            if not receipt.authoritative:
                continue
            authenticated = _authenticate_dependent_report(receipt)
            found[receipt.dependent_invocation] = _DependentInvocation(
                receipt.dependent_invocation, receipt, authenticated
            )
    for report in sorted(root.parent.glob("[1-9]*/report.json")):
        dependent = report.parent.absolute()
        if dependent == root.absolute() or dependent in found:
            continue
        if _report_references_origin(report, manifests):
            found[dependent] = _DependentInvocation(dependent, None, True)
    for projection in sorted(root.parent.glob("[1-9]*/targets/*/simulation.json")):
        dependent = projection.parents[2].absolute()
        if dependent == root.absolute() or dependent in found:
            continue
        if _projection_references_origin(projection, manifests):
            found[dependent] = _DependentInvocation(dependent, None, True)
    if reject_unknown:
        _reject_unknown_producers(root, found)
    return tuple(found[path] for path in sorted(found, key=str))


def _authenticate_dependent_report(receipt: CampaignDependencyReceipt) -> bool:
    report = receipt.dependent_invocation / "report.json"
    if not report.is_file():
        return _completed_tombstone(receipt.dependent_invocation)
    return _report_references_campaign(
        report,
        receipt.origin_target / "campaign/manifest.json",
        receipt.campaign_id,
        external_origin_target=receipt.origin_target,
    )


def _report_references_origin(report: Path, manifests: tuple[Path, ...]) -> bool:
    for manifest in manifests:
        try:
            document = _read_object(manifest)
            campaign_id = require_uuid4(document.get("campaign_id"), field="campaign_id")
            if _report_references_campaign(report, manifest, campaign_id):
                return True
        except _UnreadableDependencyError:
            raise
        except OSError as error:
            _fail_closed_on_sharing_error(error, manifest)
        except (ValueError, json.JSONDecodeError):
            continue
    return False


def _projection_references_origin(projection: Path, manifests: tuple[Path, ...]) -> bool:
    try:
        document = _read_object(projection)
        decode_simulation_projection(
            document, coverage_expected=(projection.parent / "coverage.json").is_file()
        )
    except OSError as error:
        _fail_closed_on_sharing_error(error, projection)
        return False
    except (ValueError, json.JSONDecodeError):
        return False
    try:
        reference = require_str_value(document.get("campaign_manifest"), field="campaign_manifest")
    except BoundaryError:
        return False
    candidate = Path(reference)
    if not candidate.is_absolute() or candidate != candidate.absolute():
        return False
    return candidate in {manifest.absolute() for manifest in manifests}


def _reject_unknown_producers(root: Path, found: Mapping[Path, _DependentInvocation]) -> None:
    origin_id = int(root.name)
    accounted = {int(path.name) for path in found}
    accounted.update(
        int(path.name.removeprefix(".pruned-"))
        for path in root.parent.glob(".pruned-[1-9]*")
        if path.is_dir()
        and not any(path.iterdir())
        and path.name.removeprefix(".pruned-").isdecimal()
    )
    patterns = (
        "targets/*/campaign/work-items/*/attempts/*/attempt.json",
        "targets/*/campaign/work-items/*/attempts/*/result.json",
    )
    producers: set[int] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            try:
                producer = require_int(
                    _read_object(path).get("producer_invocation_id"),
                    field="producer_invocation_id",
                )
            except (BoundaryError, OSError, ValueError, json.JSONDecodeError):
                continue
            if producer > 0:
                producers.add(producer)
    unknown = sorted(producers - accounted - {origin_id})
    if unknown:
        raise CampaignRetentionError(
            "Simulation Campaign has an unlocatable dependent producer invocation: "
            + ", ".join(str(item) for item in unknown)
        )


def _report_references_campaign(
    report_path: Path,
    manifest: Path,
    campaign_id: str,
    *,
    external_origin_target: Path | None = None,
) -> bool:
    try:
        report = _read_object(report_path)
        detail = require_dict(report.get("detail"), field="report detail")
        campaigns = require_dict(detail.get("campaigns"), field="report campaigns")
    except OSError as error:
        _fail_closed_on_sharing_error(error, report_path)
        return False
    except (BoundaryError, ValueError, json.JSONDecodeError):
        return False
    for value in campaigns.values():
        try:
            campaign = require_dict(value, field="report campaign")
            artifacts = require_dict(campaign.get("artifacts"), field="campaign artifacts")
        except BoundaryError:
            continue
        if campaign.get("campaign_id") != campaign_id:
            continue
        if _manifest_reference_matches(
            report_path,
            artifacts.get("manifest"),
            manifest,
            campaign_id,
            external_origin_target=external_origin_target,
        ):
            return True
    return False


def _manifest_reference_matches(
    report_path: Path,
    reference: object,
    manifest: Path,
    campaign_id: str,
    *,
    external_origin_target: Path | None,
) -> bool:
    try:
        decoded = decode_artifact_reference(
            reference,
            allowed_bases={"reports_root", "external_origin_target"},
        )
    except ArtifactReferenceError:
        return False
    base: Path | None = None
    if decoded.path_base == "reports_root":
        base = report_path.parent.parent.parent
    elif decoded.path_base == "external_origin_target" and external_origin_target is not None:
        base = external_origin_target
    if (
        decoded.owner != campaign_id
        or decoded.kind != "simulation_campaign_manifest"
        or base is None
    ):
        return False
    candidate = (base / decoded.path).absolute()
    try:
        contained = candidate.is_relative_to(base.absolute())
    except ValueError:
        contained = False
    if not contained or candidate != manifest.absolute():
        return False
    try:
        resolved = resolve_report_artifact_reference(
            report_path,
            reference,
            expected_kind="simulation_campaign_manifest",
            expected_owner=campaign_id,
            maximum=MANIFEST_MAX_BYTES,
            external_origin_target=external_origin_target,
            booley_dir=invocation_booley_dir(report_path.parent),
        )
    except ArtifactReferenceError:
        return False
    return resolved.path.absolute() == manifest.absolute()


def _prune_invocation(root: Path, *, project_data: Path | None = None) -> None:
    booley_dir = invocation_booley_dir(root)
    _safe_tree(root, booley_dir)
    quarantine = root.with_name(f".pruned-{root.name}")
    _safe_tree(quarantine, booley_dir)
    if root.exists():
        if quarantine.exists():
            raise CampaignRetentionError("Ambiguous invocation pruning state")
        if _matching_prune_journal(root):
            root.rename(quarantine)
        elif _empty_abandoned_reservation(root):
            write_campaign_json(
                root / ".prune.json", {"invocation": int(root.name), "operation": "full"}
            )
            root.rename(quarantine)
        else:
            _prune_published_invocation(root, project_data)
    elif not quarantine.is_dir():
        raise CampaignRetentionError("Invocation does not exist")
    children = list(quarantine.iterdir())
    if not children:
        return
    journal = quarantine / ".prune.json"
    if not journal.is_file():
        raise CampaignRetentionError("Quarantine has no pruning journal")
    for child in children:
        if child == journal:
            continue
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    journal.unlink()


def _matching_prune_journal(root: Path) -> bool:
    journal = root / ".prune.json"
    if not journal.exists():
        return False
    return _read_object(journal) == {
        "invocation": int(root.name),
        "operation": "full",
    }


def _prune_published_invocation(root: Path, project_data: Path | None) -> None:
    """Validate and quarantine one invocation that published its progress record."""
    _validate_invocation_targets(root)
    _validate_invocation_inventory(root)
    campaigns = _campaign_directories(root)
    with _campaign_mutation_locks(campaigns):
        _validate_invocation_targets(root)
        _validate_invocation_inventory(root)
        _recover_campaign_resources(root, campaigns, project_data)
        write_campaign_json(
            root / ".prune.json", {"invocation": int(root.name), "operation": "full"}
        )
    # Windows cannot rename a directory while a handle inside it (the Campaign
    # ``.lock``) is open, so rename after releasing the Campaign locks. The held
    # invocation lock still excludes producers and resumes, which lock their origin.
    root.rename(root.with_name(f".pruned-{root.name}"))


def _empty_abandoned_reservation(root: Path) -> bool:
    """Recognize a producer-locked reservation abandoned before first publication."""
    lock = root.with_name(f".invocation-{root.name}.lock")
    return root.is_dir() and not any(root.iterdir()) and _valid_lock_sentinel(lock)


def _valid_lock_sentinel(lock: Path) -> bool:
    """Accept only a regular lock file holding the empty or single-NUL sentinel.

    ``acquire_file_lock`` writes one NUL on Windows and nothing on POSIX. When a
    mandatory lock hides the byte, the at-most-one-byte length is all we can verify.
    """
    try:
        info = lock.lstat()
    except FileNotFoundError:
        return False
    if is_report_link(lock) or not stat.S_ISREG(info.st_mode) or info.st_size > 1:
        return False
    try:
        return lock.read_bytes() in {b"", b"\0"}
    except PermissionError:
        if _MANDATORY_FILE_LOCKS:
            return True
        raise


def _preflight_campaign_resources(root: Path, project_data: Path | None) -> None:
    booley_dir = invocation_booley_dir(root)
    for directory in _campaign_directories(root):
        status = inspect_retained_campaign(directory / "manifest.json", booley_dir=booley_dir)
        if status.interrupted and project_data is None:
            raise CampaignRetentionError(
                "Booley Directory is required to recover interrupted Simulation Campaign resources"
            )


def _campaign_directories(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path.parent for path in root.glob("targets/*/campaign/manifest.json") if path.is_file()
        )
    )


@contextmanager
def _campaign_mutation_locks(campaigns: tuple[Path, ...]) -> Iterator[None]:
    with ExitStack() as stack:
        for campaign in campaigns:
            try:
                stack.enter_context(
                    retained_campaign_lock(
                        campaign / "manifest.json",
                        booley_dir=invocation_booley_dir(campaign.parents[2]),
                    )
                )
            except LockContentionError as exc:
                raise CampaignRetentionError(
                    "Simulation Campaign is being resumed; retry after it finishes"
                ) from exc
        yield


def _recover_campaign_resources(
    root: Path, campaigns: tuple[Path, ...], project_data: Path | None
) -> None:
    try:
        booley_dir = invocation_booley_dir(root)
        # Only this invocation's own lanes and run directories can survive.
        resolved = project_data or _infer_project_data(root)
        for directory in campaigns:
            status = inspect_retained_campaign(directory / "manifest.json", booley_dir=booley_dir)
            recover_retained_campaign_resources(status, resolved, booley_dir=booley_dir)
    except CampaignRetentionError:
        raise
    except (OSError, ValueError, SimulationCampaignIntegrityError) as exc:
        raise CampaignRetentionError(f"Campaign recovery is invalid: {exc}") from exc


def _infer_project_data(root: Path) -> Path | None:
    reports_root = root.parent.parent
    if reports_root.name == "flow-reports" and reports_root.parent.name == ".runtime":
        return reports_root.parent.parent
    if reports_root.name == "flow-reports":
        from booley.runtime.booley_dir import resolve_booley_dir

        candidate = reports_root.parent
        try:
            selected = resolve_booley_dir().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        # Compare resolved, but return the lexical entry so mirrors keep one spelling.
        if candidate.resolve() == selected:
            return candidate
    return None


def _validate_invocation_targets(root: Path) -> None:
    progress = _read_object(root / "progress.json")
    # Reject contradictory progress; a live producer is excluded by the invocation lock.
    # Nonterminal progress under a free producer lock means the producer died before
    # terminalizing it, so the invocation is abandoned and full pruning may proceed.
    try:
        validate_progress_shape(progress)
    except ValueError as exc:
        raise CampaignRetentionError(f"Invocation progress is invalid: {exc}") from exc
    targets = cast(list[str], progress["targets"])
    if not targets:
        raise CampaignRetentionError("Invocation has no exact Target manifest")
    if any(not target or target in {".", ".."} for target in targets):
        raise CampaignRetentionError("Invocation has ambiguous Targets")
    completed = cast(list[str], progress["completed_targets"])
    pending = cast(list[str], progress["pending_targets"])
    if any(not target_report_directory(root, name).is_dir() for name in completed) and not (
        _is_resume_receipt(root, completed, pending)
    ):
        raise CampaignRetentionError("Invocation contains an unresolved completed Target")
    expected = {target_report_directory(root, target).name for target in targets}
    directories = (root / "targets").iterdir() if (root / "targets").exists() else ()
    for directory in directories:
        if directory.name not in expected:
            raise CampaignRetentionError("Invocation contains an unresolved Target")
        # Partial attempts can be removed; existing Simulation Campaigns must agree.
        if (directory / "coverage.json").exists():
            selector = next(
                target for target in targets if target_report_directory(root, target) == directory
            )
            _target(root, selector, require_projection=False)
        campaign = directory / "campaign"
        if (campaign / "manifest.json").exists():
            _inspect_simulation_campaign(directory, campaign)


def _validate_invocation_inventory(root: Path) -> None:
    progress = _read_object(root / "progress.json")
    targets = progress["targets"]
    assert isinstance(targets, list)
    expected = {(root / "progress.json").absolute()}
    batch = root / ".prune-batch.json"
    if batch.exists():
        if _read_prune_batch(root) is None:
            raise CampaignRetentionError("Pruning batch journal is invalid")
        expected.add(batch.absolute())
    progress_lock = root / ".progress.lock"
    if progress_lock.exists():
        if not _valid_lock_sentinel(progress_lock):
            raise CampaignRetentionError(
                f"Invocation progress lock content is invalid: {progress_lock}"
            )
        expected.add(progress_lock.absolute())
    report = root / "report.json"
    if report.exists():
        if _read_object(report).get("flow") != "sim":
            raise CampaignRetentionError(
                f"Invocation report does not belong to Simulation: {report}"
            )
        expected.add(report.absolute())
    for selector in targets:
        assert isinstance(selector, str)
        target = target_report_directory(root, selector)
        expected.update(_target_owned_files(root, target, selector))
        simulation = target / "simulation.json"
        if simulation.exists():
            expected.update(_referenced_invocation_artifacts(root, _read_object(simulation)))
    expected.update(_referenced_invocation_artifacts(root, progress))
    unexpected = sorted(
        path.absolute()
        for path in root.rglob("*")
        if path.is_file() and path.absolute() not in expected
    )
    if unexpected:
        raise CampaignRetentionError(f"Unrecognized invocation content: {unexpected[0]}")


def _referenced_invocation_artifacts(root: Path, document: Mapping[str, object]) -> set[Path]:
    """Resolve only exact invocation-owned files cited by artifact blocks."""
    references: list[str] = []

    def visit(value: object, *, in_artifacts: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(item, in_artifacts=in_artifacts or key == "artifacts")
        elif isinstance(value, list):
            for item in value:
                visit(item, in_artifacts=in_artifacts)
        elif in_artifacts and isinstance(value, str):
            references.append(value)

    visit(document)
    owned: set[Path] = set()
    for reference in references:
        parts = Path(reference).parts
        try:
            marker = parts.index("artifacts")
        except ValueError:
            continue
        relative = Path(*parts[marker:])
        candidate = root / relative
        _validate_invocation_artifact_layout(relative)
        if candidate.is_symlink() or not candidate.is_file():
            raise CampaignRetentionError(
                f"Invocation artifact reference is missing or unsafe: {reference}"
            )
        owned.add(candidate.absolute())
    return owned


def _validate_invocation_artifact_layout(relative: Path) -> None:
    parts = relative.parts
    role_offset = 2 if len(parts) > 1 and parts[1] == "candidate" else 3
    if len(parts) < role_offset + 4 or parts[0] != "artifacts":
        raise CampaignRetentionError(f"Invalid invocation artifact layout: {relative}")
    if parts[1] not in {"candidate", "baseline"}:
        raise CampaignRetentionError(f"Invalid invocation artifact role: {relative}")
    tail = parts[role_offset:]
    if len(tail) not in {4, 5} or tail[1] != "tests":
        raise CampaignRetentionError(f"Invalid invocation artifact layout: {relative}")
    if len(tail) == 4 and tail[-1] != "run.log":
        raise CampaignRetentionError(f"Invalid invocation artifact file: {relative}")
    if len(tail) == 5:
        expected_names = {
            "result": "result.json",
            "results_xml": "results.xml",
            "cocotb_results_json": "cocotb_results.json",
            "trace_status": "trace_status.json",
            "trace_incident": "trace_incident.txt",
        }
        kind, filename = tail[-2:]
        if kind == "trace":
            valid = Path(filename).suffix.lower() in {".fst", ".vcd"}
        else:
            valid = expected_names.get(kind) == filename
        if not valid:
            raise CampaignRetentionError(f"Invalid invocation artifact file: {relative}")
    if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in parts):
        raise CampaignRetentionError(f"Unsafe invocation artifact path: {relative}")


def _target_owned_files(root: Path, target: Path, selector: str) -> set[Path]:
    expected = {(target / "simulation.json").absolute()}
    public = target / "coverage.json"
    if public.exists():
        storage, loaded = _target(root, selector, require_projection=False)
        expected.update(_coverage_owned_files(target, storage, loaded))
    campaign = target / "campaign"
    manifest = campaign / "manifest.json"
    if manifest.exists():
        booley_dir = invocation_booley_dir(root)
        status = inspect_retained_campaign(manifest, booley_dir=booley_dir)
        expected.update(status.retention_files)
        expected.update(authenticate_retained_campaign_inventory(status, booley_dir=booley_dir))
        try:
            expected.update(dependency_receipt_files(manifest))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CampaignRetentionError(
                f"Simulation Campaign dependency registry is invalid: {exc}"
            ) from exc
        for coverage in status.coverage_directories:
            expected.update(_campaign_coverage_files(coverage))
        lock = campaign / ".lock"
        if lock.exists():
            if not _valid_lock_sentinel(lock):
                raise CampaignRetentionError(f"Campaign lock content is invalid: {lock}")
            expected.add(lock.absolute())
    return expected


def _campaign_coverage_files(coverage: Path) -> set[Path]:
    campaign_path = coverage / "coverage.json"
    if campaign_path.exists():
        loaded = load_coverage_campaign(campaign_path)
        return _coverage_owned_files(coverage, coverage, loaded)
    if not coverage.exists():
        return set()
    marker = coverage / ".collection-started"
    if marker.exists() and marker.read_bytes() != b"":
        raise CampaignRetentionError(f"Coverage collection marker changed: {marker}")
    return {
        marker.absolute(),
        *(path.absolute() for path in (coverage / "native").rglob("*") if path.is_file()),
        *(
            (coverage / name).absolute()
            for name in (
                "declarations/inventory.json",
                "build-evidence/cells.tree.json",
                "build-evidence/tree.meta.json",
            )
            if marker.exists() and (coverage / name).is_file()
        ),
    }


def _coverage_owned_files(
    target: Path, storage: Path, loaded: LoadedCoverageCampaign
) -> set[Path]:
    markers = {target / ".collection-started", storage / ".collection-started"}
    for marker in markers:
        if marker.exists() and marker.read_bytes() != b"":
            raise CampaignRetentionError(f"Coverage collection marker changed: {marker}")
    availability = storage / "availability.json"
    expected = {
        (target / "coverage.json").absolute(),
        (storage / "coverage.json").absolute(),
        availability.absolute(),
        *(marker.absolute() for marker in markers),
    }
    point_store = loaded.summary.point_store
    if point_store is not None:
        expected.add((storage / point_store.path).absolute())
    expected.update((storage / artifact.path).absolute() for artifact in loaded.campaign.artifacts)
    return expected


def _availability_document(loaded: LoadedCoverageCampaign, *, status: str) -> dict[str, object]:
    campaign = loaded.campaign
    point_store = loaded.summary.point_store
    return {
        "$schema": "booley.coverage-availability/v1",
        "campaign_id": campaign.campaign_id,
        "campaign_sha256": loaded.summary.manifest_sha256.removeprefix("sha256:"),
        "point_store_sha256": point_store.sha256 if point_store is not None else None,
        "artifacts": _native_artifacts(campaign),
        "status": status,
    }


def _inspect_simulation_campaign(target: Path, campaign: Path) -> _CampaignEligibility:
    try:
        status = inspect_retained_campaign(
            campaign / "manifest.json", booley_dir=invocation_booley_dir(target.parents[1])
        )
    except UnsupportedSimulationCampaignFormatError as exc:
        raise CampaignRetentionError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise CampaignRetentionError(
            f"Simulation Campaign is invalid and cannot be pruned: {exc}"
        ) from exc
    incomplete_recovery = bool(status.pending or status.interrupted)
    if status.summary_present and not status.summary_completed_matches:
        raise CampaignRetentionError("Simulation Campaign acceptance projections are incomplete")
    if status.summary_present and status.summary_complete == incomplete_recovery:
        raise CampaignRetentionError("Simulation Campaign acceptance projections are incomplete")
    projection_path = target / "simulation.json"
    projection = None
    if projection_path.exists():
        try:
            projection = decode_simulation_projection(
                _read_object(projection_path),
                coverage_expected=(target / "coverage.json").is_file(),
            )
        except UnsupportedSimulationCampaignFormatError as exc:
            raise CampaignRetentionError(str(exc.located(target.parents[1]))) from exc
        except (OSError, ValueError) as exc:
            raise CampaignRetentionError(
                f"Simulation Campaign is invalid and cannot be pruned: {exc}"
            ) from exc
        _validate_projection_identity(target, status, projection)
    abandoned = incomplete_recovery or not status.summary_present or projection is None
    if projection is not None and projection.document.get("complete") is (not abandoned):
        return _CampaignEligibility(status, abandoned)
    if projection is None and abandoned:
        return _CampaignEligibility(status, True)
    raise CampaignRetentionError("Simulation Campaign acceptance projections are incomplete")


def _validate_native_pruning_eligibility(root: Path, selector: str) -> None:
    if _empty_abandoned_reservation(root):
        raise CampaignRetentionError(
            "native-only pruning requires a completed Target; use --full to discard "
            "this abandoned invocation"
        )
    progress = _read_object(root / "progress.json")
    targets = progress.get("targets")
    if (
        progress.get("flow") != "sim"
        or not isinstance(targets, list)
        or targets.count(selector) != 1
    ):
        raise CampaignRetentionError("Target does not resolve exactly in this invocation")
    target = target_report_directory(root, selector)
    manifest = target / "campaign" / "manifest.json"
    if not manifest.exists():
        if not (target / "coverage.json").exists():
            raise CampaignRetentionError(
                "native-only pruning requires a completed Target; use --full to discard "
                "this abandoned invocation"
            )
        return
    eligibility = _inspect_simulation_campaign(target, manifest.parent)
    status = eligibility.status
    workload = cast(Mapping[str, object], status.manifest.document["workload"])
    if workload["coverage"] is not True:
        raise CampaignRetentionError("--native-target has no native Coverage Campaign")
    if eligibility.abandoned:
        raise CampaignRetentionError(
            "native-only pruning requires a completed Target; use --full to discard the "
            f"abandoned invocation or booley flow sim --resume-from {manifest} to preserve "
            "and complete it"
        )


def _validate_projection_identity(
    target: Path, status: RetainedCampaignStatus, projection: SimulationProjection
) -> None:
    expected = {
        "target": status.target_selector,
        "target_identity": status.target_identity,
    }
    if any(projection.document.get(key) != value for key, value in expected.items()):
        raise CampaignRetentionError("Simulation Campaign projection identity disagrees")
    if projection.trust is ProjectionTrust.TARGET_CONSISTENT_LEGACY and (
        type(projection.document.get("passed")) is not bool
        or type(projection.document.get("inconclusive")) is not bool
        or not isinstance(projection.document.get("tests"), list)
    ):
        raise CampaignRetentionError("legacy Simulation verdict projection is invalid")
    if projection.trust is ProjectionTrust.AUTHENTICATED:
        _validate_authenticated_projection(target, status, projection.document)


def _validate_authenticated_projection(
    target: Path, status: RetainedCampaignStatus, document: Mapping[str, object]
) -> None:
    if document.get("campaign_id") != status.campaign_id:
        raise CampaignRetentionError("Simulation Campaign projection owner disagrees")
    booley_dir = invocation_booley_dir(target.parents[1])
    artifact = resolve_artifact_reference(
        document.get("campaign_manifest"),
        bases={"origin_target": target},
        allowed_bases={"origin_target"},
        expected_kind="simulation_campaign_manifest",
        expected_owner=status.campaign_id,
        maximum=MANIFEST_MAX_BYTES,
        booley_dir=booley_dir,
    )
    if artifact.path != status.manifest_path:
        raise CampaignRetentionError("Simulation Campaign projection manifest disagrees")
    if not (target / "coverage.json").is_file():
        return
    coverage = resolve_artifact_reference(
        document.get("coverage_campaign"),
        bases={"origin_target": target},
        allowed_bases={"origin_target"},
        expected_kind="coverage_campaign_reference",
        expected_owner=status.campaign_id,
        maximum=MAX_REFERENCE_BYTES,
        booley_dir=booley_dir,
    )
    if coverage.path != (target / "coverage.json").absolute():
        raise CampaignRetentionError(
            "Simulation Campaign projection Coverage Campaign reference disagrees"
        )


def _is_resume_receipt(root: Path, completed: list[str], pending: list[str]) -> bool:
    """Recognize a complete report-only resume without granting storage authority."""
    if pending or not completed or (root / "targets").exists():
        return False
    try:
        report = _read_object(root / "report.json")
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    detail = report.get("detail")
    campaigns = detail.get("campaigns") if isinstance(detail, dict) else None
    return (
        report.get("flow") == "sim"
        and report.get("exit_code") in {0, 1, 2}
        and isinstance(campaigns, dict)
        and set(campaigns) == set(completed)
    )


def main() -> None:
    """Internal maintenance CLI; requires an explicit report root and exact selection."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-root", required=True, type=Path)
    parser.add_argument("--invocation", required=True, type=int)
    parser.add_argument(
        "--project-data",
        type=Path,
        help=(
            "Resolved Booley Directory root, needed by --full only when an interrupted lane "
            "or owned run directory must be recovered; not used by --native-target"
        ),
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--native-target", help="Exact Target selector for native-only pruning")
    operation.add_argument(
        "--full",
        action="store_true",
        help=(
            "Remove the full invocation after cancelling interrupted lane executions and "
            "marker-checked cleanup of owned templated run directories"
        ),
    )
    parser.add_argument(
        "--include-dependents",
        action="store_true",
        help="With --full, also remove authenticated resume dependents before the origin",
    )
    args = parser.parse_args()
    if args.include_dependents and not args.full:
        parser.error("--include-dependents is only valid with --full")
    try:
        if args.full:
            prune_invocation(
                args.reports_root,
                args.invocation,
                project_data=args.project_data,
                include_dependents=args.include_dependents,
            )
        else:
            prune_native_payload(args.reports_root, args.invocation, args.native_target)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Campaign pruning failed: {exc}\n")


if __name__ == "__main__":
    main()
