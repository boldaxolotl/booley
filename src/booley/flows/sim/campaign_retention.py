"""Exact Campaign retention operations; normalized evidence is immutable.

Native removal commits by renaming the whole native directory. The availability
journal precedes that rename, so an interrupted cleanup is explicit and retryable.
Full removal leaves an empty numbered tombstone to prevent invocation-id reuse.
"""

import hashlib
import json
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from .campaign import (
    ProjectionTrust,
    RetainedCampaignStatus,
    SimulationProjection,
    decode_simulation_projection,
    resolve_artifact_reference,
)
from .campaign.codec import MANIFEST_MAX_BYTES
from .campaign_reports import (
    campaign_invocation_lock,
    is_report_link,
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


def _remove_tree(path: Path) -> None:
    """Remove an authenticated artifact tree, including read-only snapshots."""
    for child in path.rglob("*"):
        if child.is_file():
            child.chmod(child.stat().st_mode | stat.S_IWUSR)
    path.chmod(path.stat().st_mode | stat.S_IWUSR)
    shutil.rmtree(path)


def _safe_tree(path: Path) -> None:
    for parent in (path, *path.parents):
        if is_report_link(parent):
            raise CampaignRetentionError(f"Symlinks are not allowed: {parent}")
    if path.is_dir():
        for child in path.rglob("*"):
            if is_report_link(child) or not (child.is_file() or child.is_dir()):
                raise CampaignRetentionError(f"Unsafe artifact: {child}")


def _invocation(reports_root: Path, invocation: int) -> Path:
    if type(invocation) is not int or invocation < 1:
        raise CampaignRetentionError("Select one exact positive invocation number")
    root = Path(reports_root).absolute() / "sim" / str(invocation)
    _safe_tree(root)
    return root


def _read_object(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CampaignRetentionError(f"Expected an object: {path}")
    return document


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


def _native_manifest(target: Path, campaign: CoverageCampaign) -> list[dict[str, object]]:
    artifacts = _native_artifacts(campaign)
    known = {str(item["path"]): item["sha256"] for item in artifacts}
    for name in ("native", ".native-pruned"):
        directory = target / name
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            relative = "native/" + path.relative_to(directory).as_posix()
            if relative not in known:
                raise CampaignRetentionError(f"Unrecognized native payload: {path}")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if known[relative] and f"sha256:{digest}" != known[relative]:
                raise CampaignRetentionError(f"Native payload changed since collection: {path}")
    return artifacts


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
    """Remove one exact Target's native databases; return its availability sidecar."""
    root = _invocation(reports_root, invocation)
    with campaign_invocation_lock(root):
        return _prune_native(root, target)


def _prune_native(root: Path, target: str) -> Path:
    _safe_tree(root)
    directory, loaded = _target(root, target)
    campaign = loaded.campaign
    _native_manifest(directory, campaign)
    sidecar = directory / "availability.json"
    document = _availability_document(loaded, status="pruning")
    if sidecar.exists():
        previous = _read_object(sidecar)
        if (
            previous.get("status") not in {"pruning", "pruned"}
            or {**previous, "status": "pruning"} != document
        ):
            raise CampaignRetentionError("Availability journal does not match this Campaign")
    else:
        _validate_unpruned(directory, campaign)
    native, quarantine = directory / "native", directory / ".native-pruned"
    if native.exists() and quarantine.exists():
        raise CampaignRetentionError("Ambiguous native pruning state")
    write_campaign_json(sidecar, document)
    if native.exists():
        native.rename(quarantine)
    if quarantine.exists():
        _remove_tree(quarantine)
    write_campaign_json(sidecar, {**document, "status": "pruned"})
    return sidecar


def _validate_unpruned(directory: Path, campaign: CoverageCampaign) -> None:
    if (directory / ".native-pruned").exists():
        raise CampaignRetentionError("Native quarantine has no availability journal")
    for artifact in campaign.artifacts:
        if (
            artifact.kind in {"raw_native", "merged_native"}
            and artifact.sha256
            and not (directory / artifact.path).is_file()
        ):
            raise CampaignRetentionError(
                f"Native payload is unexpectedly missing: {artifact.path}"
            )


def prune_invocation(
    reports_root: Path, invocation: int, *, project_data: Path | None = None
) -> None:
    """Remove exactly one invocation, retaining only an empty number tombstone."""
    root = _invocation(reports_root, invocation)
    with campaign_invocation_lock(root):
        _prune_invocation(root, project_data=project_data)


def _prune_invocation(root: Path, *, project_data: Path | None = None) -> None:
    _safe_tree(root)
    quarantine = root.with_name(f".pruned-{root.name}")
    _safe_tree(quarantine)
    if root.exists():
        if quarantine.exists():
            raise CampaignRetentionError("Ambiguous invocation pruning state")
        _validate_invocation_targets(root)
        _validate_invocation_inventory(root)
        _release_child_indexes(root, project_data)
        write_campaign_json(
            root / ".prune.json", {"invocation": int(root.name), "operation": "full"}
        )
        root.rename(quarantine)
    elif not quarantine.is_dir():
        raise CampaignRetentionError("Invocation does not exist")
    children = list(quarantine.iterdir())
    if not children:
        return
    journal = quarantine / ".prune.json"
    if not journal.is_file() or _read_object(journal) != {
        "invocation": int(root.name),
        "operation": "full",
    }:
        raise CampaignRetentionError("Quarantine has no matching pruning journal")
    for child in children:
        if child == journal:
            continue
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    journal.unlink()


def _release_child_indexes(root: Path, project_data: Path | None) -> None:
    from booley.runtime.execution_records import (
        release_retired_campaign_children,
        retained_campaign_child_manifests,
        validate_retired_campaign_children,
    )

    target_root = root / "targets"
    campaign_children = tuple(target_root.glob("*/campaign/child-executions"))
    if not campaign_children:
        return
    try:
        for children in campaign_children:
            ownership_root = _child_ownership_root(children, retained_campaign_child_manifests)
            if ownership_root is None:
                validate_retired_campaign_children(children)
            else:
                inferred = _infer_project_data(ownership_root)
                if inferred is None:
                    raise CampaignRetentionError(
                        "Simulation Campaign child ownership cannot resolve its Project data"
                    )
                resolved = Path(project_data).resolve() if project_data else inferred
                if resolved != inferred.resolve():
                    raise CampaignRetentionError(
                        "Project data disagrees with the canonical Simulation Campaign owner"
                    )
                release_retired_campaign_children(resolved, children)
    except (OSError, ValueError) as exc:
        raise CampaignRetentionError(
            f"Simulation Campaign child retention is invalid: {exc}"
        ) from exc


def _child_ownership_root(children: Path, manifest_paths) -> Path | None:
    """Return the canonical invocation root, or classify this mirror as detached."""
    local = children.parent / "manifest.json"
    producer_paths = manifest_paths(children)
    matches: list[bool] = []
    for producer in producer_paths:
        try:
            matches.append(producer.samefile(local))
        except OSError:
            matches.append(False)
    if matches and all(matches):
        return producer_paths[0].parents[3]
    if any(matches):
        raise CampaignRetentionError("Simulation Campaign child ownership is ambiguous")
    return None


def _infer_project_data(root: Path) -> Path | None:
    reports_root = root.parent.parent
    if reports_root.name == "flow-reports" and reports_root.parent.name == ".runtime":
        return reports_root.parent.parent
    if reports_root.name == "flow-reports":
        from booley.runtime.project_dir import resolve_project_dir

        candidate = reports_root.parent.resolve()
        try:
            selected = resolve_project_dir().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if candidate == selected:
            return candidate
    return None


def _validate_invocation_targets(root: Path) -> None:
    progress = _read_object(root / "progress.json")
    targets = progress.get("targets")
    if progress.get("flow") != "sim" or not isinstance(targets, list) or not targets:
        raise CampaignRetentionError("Invocation has no exact Target manifest")
    if any(
        not isinstance(target, str) or not target or target in {".", ".."} for target in targets
    ) or len(set(targets)) != len(targets):
        raise CampaignRetentionError("Invocation has ambiguous Targets")
    phase = progress.get("phase")
    complete = progress.get("complete")
    if complete is not True or phase not in {"complete", "aborted", "superseded"}:
        raise CampaignRetentionError("Invocation progress is not terminal")
    _validate_completed_targets(root, progress, targets)
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
            _validate_simulation_campaign(
                directory,
                campaign,
                allow_incomplete=phase in {"aborted", "superseded"},
            )


def _validate_invocation_inventory(root: Path) -> None:
    progress = _read_object(root / "progress.json")
    targets = progress["targets"]
    assert isinstance(targets, list)
    expected = {(root / "progress.json").absolute()}
    progress_lock = root / ".progress.lock"
    if progress_lock.exists():
        if progress_lock.is_symlink() or progress_lock.read_bytes() not in {b"", b"\0"}:
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
    unexpected = sorted(
        path.absolute()
        for path in root.rglob("*")
        if path.is_file() and path.absolute() not in expected
    )
    if unexpected:
        raise CampaignRetentionError(f"Unrecognized invocation content: {unexpected[0]}")


def _target_owned_files(root: Path, target: Path, selector: str) -> set[Path]:
    expected = {(target / "simulation.json").absolute()}
    public = target / "coverage.json"
    if public.exists():
        storage, loaded = _target(root, selector, require_projection=False)
        expected.update(_coverage_owned_files(target, storage, loaded))
    campaign = target / "campaign"
    manifest = campaign / "manifest.json"
    if manifest.exists():
        from .campaign import inspect_retained_campaign

        status = inspect_retained_campaign(manifest)
        expected.update(status.retention_files)
        for coverage in status.coverage_directories:
            expected.update(_campaign_coverage_files(coverage))
        lock = campaign / ".lock"
        if lock.exists():
            if lock.read_bytes() not in {b"", b"\0"}:
                raise CampaignRetentionError(f"Campaign lock content is invalid: {lock}")
            expected.add(lock.absolute())
        for registry in ("entries", "retired", "released"):
            expected.update(
                path.absolute()
                for path in (campaign / "child-executions" / registry).glob("*.json")
            )
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
    }


def _coverage_owned_files(
    target: Path, storage: Path, loaded: LoadedCoverageCampaign
) -> set[Path]:
    markers = {target / ".collection-started", storage / ".collection-started"}
    for marker in markers:
        if marker.exists() and marker.read_bytes() != b"":
            raise CampaignRetentionError(f"Coverage collection marker changed: {marker}")
    availability = storage / "availability.json"
    if availability.exists():
        _validate_availability(availability, loaded)
    expected = {
        (target / "coverage.json").absolute(),
        (storage / "coverage.json").absolute(),
        availability.absolute(),
        *(marker.absolute() for marker in markers),
    }
    point_store = loaded.summary.point_store
    if point_store is not None:
        expected.add((storage / point_store.path).absolute())
    for artifact in loaded.campaign.artifacts:
        relative = Path(artifact.path)
        expected.add((storage / relative).absolute())
        if relative.parts and relative.parts[0] == "native":
            expected.add((storage / ".native-pruned" / Path(*relative.parts[1:])).absolute())
    return expected


def _validate_availability(path: Path, loaded: LoadedCoverageCampaign) -> None:
    expected = _availability_document(loaded, status="pruning")
    document = _read_object(path)
    status = document.get("status")
    if status not in {"pruning", "pruned"}:
        raise CampaignRetentionError("Availability journal does not match this Campaign")
    expected["status"] = status
    if document != expected:
        raise CampaignRetentionError("Availability journal does not match this Campaign")


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


def _validate_simulation_campaign(
    target: Path, campaign: Path, *, allow_incomplete: bool = False
) -> None:
    from .campaign import inspect_retained_campaign

    try:
        status = inspect_retained_campaign(campaign / "manifest.json")
        projection = decode_simulation_projection(
            _read_object(target / "simulation.json"),
            coverage_expected=(target / "coverage.json").is_file(),
        )
    except (OSError, ValueError) as exc:
        raise CampaignRetentionError(
            f"Simulation Campaign is invalid and cannot be pruned: {exc}"
        ) from exc
    if (status.pending or status.interrupted) and allow_incomplete:
        return
    if status.pending or status.interrupted:
        raise CampaignRetentionError("Incomplete Simulation Campaigns cannot be pruned")
    if (
        not status.summary_complete
        or not status.summary_completed_matches
        or projection.document.get("complete") is not True
    ):
        raise CampaignRetentionError("Simulation Campaign acceptance projections are incomplete")
    _validate_projection_identity(target, status, projection)


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
    artifact = resolve_artifact_reference(
        document.get("campaign_manifest"),
        bases={"origin_target": target},
        allowed_bases={"origin_target"},
        expected_kind="simulation_campaign_manifest",
        expected_owner=status.campaign_id,
        maximum=MANIFEST_MAX_BYTES,
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
    )
    if coverage.path != (target / "coverage.json").absolute():
        raise CampaignRetentionError(
            "Simulation Campaign projection Coverage Campaign reference disagrees"
        )


def _validate_completed_targets(
    root: Path, progress: dict[str, object], targets: list[str]
) -> None:
    completed = progress.get("completed_targets")
    pending = progress.get("pending_targets")
    for names in (completed, pending):
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise CampaignRetentionError(
                "Invocation has invalid completed Target or pending Target lists"
            )
        if len(set(names)) != len(names):
            raise CampaignRetentionError(
                "Invocation repeats completed Target or pending Target identities"
            )
    assert isinstance(completed, list) and isinstance(pending, list)
    if set(completed) & set(pending) or set(completed) | set(pending) != set(targets):
        raise CampaignRetentionError("Invocation has inconsistent completed Target resolution")
    if any(not target_report_directory(root, name).is_dir() for name in completed) and not (
        _is_resume_receipt(root, completed, pending)
    ):
        raise CampaignRetentionError("Invocation contains an unresolved completed Target")


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
            "Resolved project-data root used only to verify canonical ownership for "
            "--full; it never grants a detached copy authority to release shared "
            "indexes and is not required for --native-target"
        ),
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--native-target", help="Exact Target selector for native-only pruning")
    operation.add_argument("--full", action="store_true", help="Remove the full invocation")
    args = parser.parse_args()
    try:
        if args.full:
            prune_invocation(args.reports_root, args.invocation, project_data=args.project_data)
        else:
            prune_native_payload(args.reports_root, args.invocation, args.native_target)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Campaign pruning failed: {exc}\n")


if __name__ == "__main__":
    main()
