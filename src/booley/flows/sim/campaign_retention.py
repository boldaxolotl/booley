"""Exact Campaign retention operations; normalized evidence is immutable.

Native removal commits by renaming the whole native directory. The availability
journal precedes that rename, so an interrupted cleanup is explicit and retryable.
Full removal leaves an empty numbered tombstone to prevent invocation-id reuse.
"""

import hashlib
import json
import shutil
from pathlib import Path

from .campaign_reports import (
    campaign_invocation_lock,
    is_report_link,
    target_report_directory,
    write_campaign_json,
)
from .coverage_campaign import CoverageCampaign, DurableTargetIdentity
from .coverage_campaign_store import load_coverage_campaign


class CampaignRetentionError(ValueError):
    """A selection or filesystem tree cannot be safely pruned."""


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
) -> tuple[Path, CoverageCampaign]:
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
    document = _read_object(target / "coverage.json")
    identity = document.get("target")
    if not isinstance(identity, dict) or not isinstance(identity.get("identity"), str):
        raise CampaignRetentionError("Campaign has no durable Target identity")
    campaign = load_coverage_campaign(
        target / "coverage.json", DurableTargetIdentity(identity["identity"])
    ).campaign
    if campaign.target.selector != selector or campaign.invocation["id"] != int(root.name):
        raise CampaignRetentionError("Campaign identity disagrees with the exact selection")
    if not require_projection:
        return target, campaign
    projection = _read_object(target / "simulation.json")
    if (
        projection.get("target_identity") != campaign.target.identity
        or projection.get("complete") is not True
    ):
        raise CampaignRetentionError(
            "Simulation projection is missing or belongs to another Target"
        )
    return target, campaign


def _native_manifest(target: Path, campaign: CoverageCampaign) -> list[dict[str, object]]:
    artifacts = []
    for artifact in campaign.artifacts:
        if artifact.kind not in {"raw_native", "merged_native"}:
            continue
        path = Path(artifact.path)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "native":
            raise CampaignRetentionError("Native artifact is outside the Target native directory")
        artifacts.append({"id": artifact.id, "path": artifact.path, "sha256": artifact.sha256})
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


def prune_native_payload(reports_root: Path, invocation: int, target: str) -> Path:
    """Remove one exact Target's native databases; return its availability sidecar."""
    root = _invocation(reports_root, invocation)
    with campaign_invocation_lock(root):
        return _prune_native(root, target)


def _prune_native(root: Path, target: str) -> Path:
    _safe_tree(root)
    directory, campaign = _target(root, target)
    artifacts = _native_manifest(directory, campaign)
    manifest = _read_object(directory / "coverage.json")
    point_store = manifest.get("point_store")
    point_store_sha256 = point_store.get("sha256") if isinstance(point_store, dict) else None
    sidecar = directory / "availability.json"
    document = {
        "$schema": "booley.coverage-availability/v1",
        "campaign_id": campaign.campaign_id,
        "campaign_sha256": hashlib.sha256((directory / "coverage.json").read_bytes()).hexdigest(),
        "point_store_sha256": point_store_sha256,
        "artifacts": artifacts,
        "status": "pruning",
    }
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
        shutil.rmtree(quarantine)
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


def prune_invocation(reports_root: Path, invocation: int) -> None:
    """Remove exactly one invocation, retaining only an empty number tombstone."""
    root = _invocation(reports_root, invocation)
    with campaign_invocation_lock(root):
        _prune_invocation(root)


def _prune_invocation(root: Path) -> None:
    _safe_tree(root)
    quarantine = root.with_name(f".pruned-{root.name}")
    _safe_tree(quarantine)
    if root.exists():
        if quarantine.exists():
            raise CampaignRetentionError("Ambiguous invocation pruning state")
        _validate_invocation_targets(root)
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
            shutil.rmtree(child)
        else:
            child.unlink()
    journal.unlink()


def _validate_invocation_targets(root: Path) -> None:
    progress = _read_object(root / "progress.json")
    targets = progress.get("targets")
    if progress.get("flow") != "sim" or not isinstance(targets, list) or not targets:
        raise CampaignRetentionError("Invocation has no exact Target manifest")
    if any(
        not isinstance(target, str) or not target or target in {".", ".."} for target in targets
    ) or len(set(targets)) != len(targets):
        raise CampaignRetentionError("Invocation has ambiguous Targets")
    _validate_completed_targets(root, progress, targets)
    expected = {target_report_directory(root, target).name for target in targets}
    directories = (root / "targets").iterdir() if (root / "targets").exists() else ()
    for directory in directories:
        if directory.name not in expected:
            raise CampaignRetentionError("Invocation contains an unresolved Target")
        # Partial attempts can be removed; existing complete Campaigns must agree.
        if (directory / "coverage.json").exists():
            selector = next(
                target for target in targets if target_report_directory(root, target) == directory
            )
            _target(root, selector, require_projection=False)


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
    if any(not target_report_directory(root, name).is_dir() for name in completed):
        raise CampaignRetentionError("Invocation contains an unresolved completed Target")


def main() -> None:
    """Internal maintenance CLI; requires an explicit report root and exact selection."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-root", required=True, type=Path)
    parser.add_argument("--invocation", required=True, type=int)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--native-target", help="Exact Target selector for native-only pruning")
    operation.add_argument("--full", action="store_true", help="Remove the full invocation")
    args = parser.parse_args()
    try:
        if args.full:
            prune_invocation(args.reports_root, args.invocation)
        else:
            prune_native_payload(args.reports_root, args.invocation, args.native_target)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Campaign pruning failed: {exc}\n")


if __name__ == "__main__":
    main()
