"""Exact Project resource claims, Docker reports and non-force cleanup.

A label describes origin; only a private acquisition receipt authorizes bytes.
Claims survive forget and retain the exact canonical identity without resolving it.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.core.private_store import PrivateStore
from booley.core.project_identity import accepted_project_labels, project_label_id, project_name_id
from booley.core.user_paths import config_dir

if TYPE_CHECKING:
    from booley.runtime.image_lifecycle import DockerPort, PreparedConvergence

LABEL_ORIGIN = "booley.project-origin"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


class ResourceError(RuntimeError):
    """Resource ownership or a complete usage observation could not be established."""


@dataclass(frozen=True, slots=True)
class Resource:
    """One observed resource; apparent size never promises reclaimable bytes."""

    name: str
    image_id: str | None = None
    size: int | str | None = None
    reason: str = "retained"
    references: tuple[str, ...] = ()


@dataclass(slots=True)
class ResourceReport:
    """Detached post-operation facts, safe to render after inventory removal."""

    images: list[Resource] = field(default_factory=list)
    volumes: list[Resource] = field(default_factory=list)
    containers: list[Resource] = field(default_factory=list)
    networks: list[Resource] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def populated(self) -> bool:
        """Whether any attributable resource remains."""
        return bool(self.images or self.volumes or self.containers or self.networks)


def _store() -> PrivateStore:
    anchor = config_dir()
    return PrivateStore(
        anchor / "eda" / "project-resources", anchor, "Project resources", ResourceError
    )


def _name(identity: str) -> str:
    return project_label_id(identity) + ".json"


def _empty(identity: str) -> dict[str, Any]:
    return {
        "version": 1,
        "project_root": identity,
        "cleanup": False,
        "aliases": {},
        "origins": {},
        "retirements": {},
        "observations": {},
    }


def _read(name: str) -> dict[str, Any]:
    store = _store()
    try:
        raw = require_dict(store.read_json(name), field="Project resources")
        identity = require_str(raw, "project_root")
        if (
            set(raw) != set(_empty(identity))
            or type(raw["version"]) is not int
            or raw["version"] != 1
            or not Path(identity).is_absolute()
            or str(Path(identity)) != identity
            or ".." in Path(identity).parts
            or name != _name(identity)
            or type(raw["cleanup"]) is not bool
        ):
            raise ResourceError("invalid Project cleanup identity")
        for key in ("aliases", "origins", "retirements", "observations"):
            rows = require_dict(raw[key], field=key)
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in rows.items()):
                raise ResourceError("invalid Project resource claim")
            ids = rows.keys() if key == "origins" else rows.values()
            if any(not _IMAGE_ID.fullmatch(image) for image in ids):
                raise ResourceError("Project resource claim requires exact image IDs")
        if any(origin != project_label_id(identity) for origin in raw["origins"].values()):
            raise ResourceError("invalid Project acquisition origin")
        _validate_claim_names(raw, identity)
        return raw
    except (BoundaryError, ValueError, OSError) as exc:
        raise ResourceError(f"cannot read Project resource proof: {exc}") from exc


def _validate_claim_names(raw: dict[str, Any], identity: str) -> None:
    from booley.runtime.session_issuance import keeper_image_for_identity

    prefix = _project_image_name(identity)

    def owned(alias: str) -> bool:
        return alias == prefix or alias.startswith(prefix + "-")

    if any(not owned(alias) for alias in raw["aliases"]):
        raise ResourceError("alias claim is outside the exact Project namespace")
    for alias, image in raw["retirements"].items():
        reference, separator, recorded = alias.rpartition("@")
        if not separator or recorded != image or not owned(reference):
            raise ResourceError("invalid Project retirement claim")
    if any(alias != keeper_image_for_identity(identity) for alias in raw["observations"]):
        raise ResourceError("invalid Project keeper observation")


def _load(identity: str) -> dict[str, Any]:
    store = _store()
    if not store.validate_existing_directory():
        return _empty(identity)
    path = store.root / _name(identity)
    if not path.exists() and not path.is_symlink():
        return _empty(identity)
    return _read(_name(identity))


def _save(raw: dict[str, Any]) -> None:
    store = _store()
    store.ensure_directory()
    name = _name(raw["project_root"])
    if not raw["cleanup"] and not any(
        raw[key] for key in ("aliases", "origins", "retirements", "observations")
    ):
        (store.root / name).unlink(missing_ok=True)
        return
    store.atomic_write_text(name, json.dumps(raw, sort_keys=True) + "\n")


def _records() -> list[dict[str, Any]]:
    store = _store()
    if not store.validate_existing_directory():
        return []
    return [_read(path.name) for path in sorted(store.root.glob("*.json"))]


def record_acquisition(identity: str, images: tuple[str, ...], docker: DockerPort) -> None:
    """Record only newly acquired IDs whose separately verified origin matches."""
    raw = _load(identity)
    origin = project_label_id(identity)
    for image in images:
        if docker.label(image, LABEL_ORIGIN) == origin:
            raw["origins"][image] = origin
    _save(raw)


def record_adoption(prepared: PreparedConvergence, *, retirement: bool = False) -> None:
    """Persist exact alias/old-ID claims before any managed tag can move."""
    if prepared.plan.project_root is None:
        return
    raw = _load(str(prepared.plan.project_root))
    prefix = _project_image_name(raw["project_root"])
    for candidate in prepared.candidates:
        if not (candidate.reference == prefix or candidate.reference.startswith(prefix + "-")):
            continue
        previous = raw["aliases"].get(candidate.reference)
        if previous is not None and previous != candidate.image_id:
            raw["retirements"][candidate.reference + "@" + previous] = previous
        raw["aliases"][candidate.reference] = candidate.image_id
    if retirement:
        for prior in prepared.prior_tags:
            if prior.image_id is not None and (
                prior.reference == prefix or prior.reference.startswith(prefix + "-")
            ):
                # A snapshot alone never creates acquisition proof.
                raw["retirements"][prior.reference + "@" + prior.image_id] = prior.image_id
    _save(raw)


def proven_origin(image: str, docker: DockerPort) -> bool:
    """Require exact created-ID receipt and its matching immutable origin label."""
    origin = docker.label(image, LABEL_ORIGIN)
    return origin is not None and any(raw["origins"].get(image) == origin for raw in _records())


def retain_cleanup_identity(identity: str) -> None:
    """Persist exact administrative identity before forgetting inventory."""
    raw = _load(identity)
    raw["cleanup"] = True
    _save(raw)


def observe_keeper(identity: str, docker: DockerPort) -> None:
    """Retain read-only attribution, separately from every deletion claim."""
    from booley.runtime.session_issuance import keeper_image_for_identity

    keeper = keeper_image_for_identity(identity)
    image = docker.image_id(keeper)
    if image is not None:
        if not _IMAGE_ID.fullmatch(image):
            raise ResourceError("keeper observation requires an exact immutable image ID")
        raw = _load(identity)
        raw["observations"][keeper] = image
        _save(raw)


def has_cleanup_identity(identity: str) -> bool:
    """Accept only the exact retained absolute spelling; never follow a path."""
    return Path(identity).is_absolute() and _load(identity)["cleanup"]


def _docker(args: list[str]) -> str:
    from booley.runtime import interactive_docker

    try:
        result = interactive_docker._run_docker(args, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResourceError(f"cannot inspect Project Docker resources: {exc}") from exc
    if result.returncode:
        raise ResourceError(f"Docker {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def _inspect(kind: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_docker([kind, "inspect", name]))
        if not isinstance(value, list) or len(value) != 1:
            raise ResourceError(f"invalid Docker {kind} inspection")
        return require_dict(value[0], field=f"Docker {kind}")
    except (ValueError, BoundaryError) as exc:
        raise ResourceError(f"invalid Docker {kind} inspection: {exc}") from exc


def _inventory(kind: str) -> dict[str, dict[str, Any]]:
    return {name: _inspect(kind, name) for name in _docker([kind, "ls", "--quiet"]).splitlines()}


def _containers() -> dict[str, dict[str, Any]]:
    result = {}
    for name in _docker(["container", "ls", "--all", "--quiet"]).splitlines():
        row = _inspect("container", name)
        image = row.get("Image")
        if not isinstance(image, str) or not _IMAGE_ID.fullmatch(image):
            raise ResourceError("container inspection lacks an exact image identity")
        _mounts(row)
        _labels(row)
        result[name] = row
    return result


def _labels(row: dict[str, Any]) -> dict[str, Any]:
    config = row.get("Config", {})
    if not isinstance(config, dict):
        raise ResourceError("invalid Docker resource configuration")
    labels = config.get("Labels", row.get("Labels", {}))
    if labels is None:
        return {}
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise ResourceError("invalid Docker resource labels")
    return labels


def _mounts(row: dict[str, Any]) -> set[str]:
    mounts = row.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(value, dict) for value in mounts):
        raise ResourceError("invalid container mounts")
    if any(
        value.get("Type") == "volume" and not isinstance(value.get("Name"), str)
        for value in mounts
    ):
        raise ResourceError("volume mount inspection lacks its exact name")
    return {value["Name"] for value in mounts if isinstance(value.get("Name"), str)}


def _sizes() -> dict[str, int | str]:
    """Disk usage is best effort; its display strings are explicitly apparent sizes."""
    try:
        raw = require_dict(
            json.loads(_docker(["system", "df", "--verbose", "--format", "{{json .}}"])),
            field="Docker disk usage",
        )
        sizes = {}
        for kind in ("Images", "Containers", "Volumes"):
            rows = raw.get(kind, [])
            if not isinstance(rows, list):
                return {}
            for row in rows:
                if isinstance(row, dict):
                    name, size = row.get("Name", row.get("ID")), row.get("Size")
                    if (
                        isinstance(name, str)
                        and isinstance(size, (str, int))
                        and not isinstance(size, bool)
                    ):
                        sizes[name] = size
        return sizes
    except (ResourceError, BoundaryError, ValueError):
        return {}


def volume_names(identity: str) -> frozenset[str]:
    """Exact all-app/all-policy state names derived from the persisted identity."""
    suffixes = ("", "-subscription", "-api_key")
    return frozenset(
        f"booley-{app}-state-{project_name_id(identity)}{suffix}"
        for app in ("claude", "codex")
        for suffix in suffixes
    )


def _project_image_name(identity: str) -> str:
    from booley.config.sandbox import SANDBOX_IMAGE

    slug = re.sub(r"[^a-z0-9_.-]+", "-", Path(identity).name.lower()).strip("-._")
    return f"{slug or 'project'}-{SANDBOX_IMAGE}-{project_label_id(identity)[:24]}"


def _candidate_ids(identity: str, docker: DockerPort) -> set[str]:
    raw = _load(identity)
    result = (
        set(raw["observations"].values())
        | set(raw["aliases"].values())
        | set(raw["retirements"].values())
        | set(raw["origins"])
    )
    from booley.runtime import image_lifecycle

    legacy_store = image_lifecycle._owned_store()
    if legacy_store.validate_existing_directory():
        for path in legacy_store.root.glob(project_label_id(identity) + "-*.json"):
            _root, _refs, owned = image_lifecycle._read_owned_record(legacy_store, path.name)
            result.update(owned)
    from booley.runtime.session_issuance import keeper_image_for_identity

    keeper = docker.image_id(keeper_image_for_identity(identity))
    if keeper is not None:
        if not _IMAGE_ID.fullmatch(keeper):
            raise ResourceError("keeper observation requires an exact immutable image ID")
        result.add(keeper)
    prefix = _project_image_name(identity)
    for row in docker.image_references():
        if row.reference == prefix or row.reference.startswith(prefix + "-"):
            result.add(row.image_id)
    for image in docker.image_ids():
        if docker.label(image, LABEL_ORIGIN) == project_label_id(identity):
            result.add(image)
    return result & docker.image_ids()


def _image_reason(image: str, docker: DockerPort) -> str:
    if not proven_origin(image, docker):
        return "unproven historical origin/acquisition"
    if image in docker.container_image_ids():
        return "container"
    if docker.repo_digests(image):
        return "registry digest"
    layers = docker.image_layers(image)
    if layers and any(
        docker.image_layers(other)[: len(layers)] == layers
        for other in docker.image_ids() - {image}
    ):
        return "child"
    if any(row.image_id == image for row in docker.image_references()):
        return "tag/keeper"
    return "unused"


def snapshot(identity: str, *, docker: DockerPort) -> ResourceReport:
    """Inspect all consumers before any caller mutates the keeper or inventory."""
    containers, volumes, networks = _containers(), _inventory("volume"), _inventory("network")
    sizes = _sizes()
    labels = accepted_project_labels(identity)
    report = ResourceReport()
    mounted = set().union(*(_mounts(row) for row in containers.values())) if containers else set()
    for image in sorted(_candidate_ids(identity, docker)):
        inspected = _inspect("image", image)
        if inspected.get("Id") != image:
            raise ResourceError("image inspection changed its immutable identity")
        size = inspected.get("Size")
        report.images.append(
            Resource(
                image,
                image,
                size if type(size) is int else sizes.get(image),
                _image_reason(image, docker),
                tuple(row.reference for row in docker.image_references() if row.image_id == image),
            )
        )
    for name in sorted(volume_names(identity) & volumes.keys()):
        report.volumes.append(
            Resource(
                name, size=sizes.get(name), reason="container" if name in mounted else "unused"
            )
        )
    for name, row in containers.items():
        if _labels(row).get("booley.project-id") in labels:
            report.containers.append(
                Resource(name, row.get("Image"), sizes.get(name), "container retained")
            )
    for name, row in networks.items():
        if _labels(row).get("booley.project-id") in labels:
            report.networks.append(Resource(name, size="N/A", reason="network retained"))
    return report


def _other_claim(image: str, identity: str, docker: DockerPort) -> bool:
    for raw in _records():
        if raw["project_root"] == identity:
            continue
        if any(
            expected == image and docker.image_id(alias) == image
            for alias, expected in raw["aliases"].items()
        ):
            return True
    return False


def _release_aliases(identity: str, docker: DockerPort, removed: list[str]) -> None:
    raw = _load(identity)
    for alias, image in list(raw["aliases"].items()):
        actual = docker.image_id(alias)
        if actual is not None and (
            actual != image
            or image in docker.container_image_ids()
            or not proven_origin(image, docker)
        ):
            continue
        if actual is not None and docker.image_id(alias) != image:
            continue
        if actual is not None:
            docker.remove_tag(alias)
            removed.append(alias)
        raw["retirements"][alias + "@" + image] = image
        del raw["aliases"][alias]
        _save(raw)


def _collect_images(identity: str, docker: DockerPort, removed: list[str]) -> None:
    raw = _load(identity)
    authorized = (
        set(raw["aliases"].values()) | set(raw["retirements"].values()) | set(raw["origins"])
    )
    candidates = _candidate_ids(identity, docker) & authorized
    # Finite captured set, repeated only to release children before parents.
    for _ in range(len(candidates)):
        progress = False
        for image in sorted(candidates & docker.image_ids()):
            if _image_reason(image, docker) == "unused" and not _other_claim(
                image, identity, docker
            ):
                docker.remove_tag(image)
                removed.append(image)
                progress = True
        if not progress:
            break


def _purge_volumes(report: ResourceReport, removed: list[str]) -> None:
    for volume in report.volumes:
        # Reinspect every container immediately before each non-force removal.
        containers = _containers()
        if any(volume.name in _mounts(row) for row in containers.values()):
            continue
        if volume.name in _inventory("volume"):
            _docker(["volume", "rm", volume.name])
            removed.append(volume.name)


def purge(identity: str, *, docker: DockerPort) -> list[str]:
    """Release exact receipt-proven aliases, then eligible leaves and state volumes."""
    observed = snapshot(identity, docker=docker)
    removed: list[str] = []
    try:
        _release_aliases(identity, docker, removed)
        _collect_images(identity, docker, removed)
        _purge_volumes(observed, removed)
        compact(docker)
    except (RuntimeError, OSError) as exc:
        try:
            left = snapshot(identity, docker=docker)
            remaining = ", ".join(row.name for row in (*left.images, *left.volumes)) or "none"
        except (RuntimeError, OSError):
            remaining = "unknown; inspection failed"
        raise ResourceError(
            f"Project purge incomplete: {exc}; already removed: "
            f"{', '.join(removed) or 'none'}; remaining: {remaining}; retry --purge"
        ) from exc
    return removed


def compact(docker: DockerPort) -> None:
    """Retain global origin proof while any extant artifact can need it."""
    existing = docker.image_ids()
    for raw in _records():
        raw["observations"] = {
            alias: image for alias, image in raw["observations"].items() if image in existing
        }
        raw["origins"] = {
            image: origin for image, origin in raw["origins"].items() if image in existing
        }
        raw["retirements"] = {
            alias: image for alias, image in raw["retirements"].items() if image in existing
        }
        raw["aliases"] = {
            alias: image for alias, image in raw["aliases"].items() if image in existing
        }
        _save(raw)


def finish_cleanup_identity(identity: str, report: ResourceReport) -> None:
    """Compact administrative identity only after a complete empty observation."""
    raw = _load(identity)
    raw["cleanup"] = report.populated()
    _save(raw)


def report_dict(report: ResourceReport) -> dict[str, Any]:
    """Return JSON-compatible detached facts."""
    return asdict(report)
