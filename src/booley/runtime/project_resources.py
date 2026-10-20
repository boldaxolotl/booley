"""Docker resources Booley creates for one Project, found by its canonical root.

Every name derives from the stored root string, so a deleted or replaced
directory still maps to the resources it created. Ownership of untagged image
bytes comes only from the image lifecycle's owned-image records.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from booley.config.sandbox import project_image_name_for_identity
from booley.core.boundary import require_dict, require_list, require_str
from booley.core.project_identity import accepted_project_labels, project_name_id
from booley.projects.image_keepers import Leftover, PurgeResult
from booley.runtime import interactive_docker
from booley.runtime.devcontainer import state_volume_names
from booley.runtime.image_lifecycle import (
    ImageReference,
    OwnedImages,
    cleanup_owned_images,
)
from booley.runtime.session_issuance import keeper_image_for_identity

# Docker prints a missing repository or tag as this placeholder.
_UNTAGGED = "<none>"


class _ImageRow(NamedTuple):
    image: str
    reference: str
    size: str


def _lines(args: list[str], *, timeout: int = 30) -> list[str]:
    output = interactive_docker.strict_docker(
        args, context=f"docker {' '.join(args[:2])}", timeout=timeout
    )
    return [line for line in output.splitlines() if line]


def _is_tag(reference: str) -> bool:
    repository, _, tag = reference.rpartition(":")
    return _UNTAGGED not in (repository, tag)


def _owns_tag(identity: str, reference: str) -> bool:
    """Whether a reference's repository is this root's keeper or managed image family."""
    family = project_image_name_for_identity(identity)
    repository = reference.rpartition(":")[0]
    return (
        repository == keeper_image_for_identity(identity).rpartition(":")[0]
        or repository == family
        or repository.startswith(family + "-")
    )


def _image_rows() -> list[_ImageRow]:
    rows = []
    for line in _lines(
        [
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{.ID}}\t{{.Repository}}:{{.Tag}}\t{{.Size}}",
        ]
    ):
        image, reference, size = line.split("\t")
        rows.append(_ImageRow(interactive_docker.validated_image_id(image), reference, size))
    return rows


def _image_reason(
    identity: str, image: str, refs: tuple[str, ...], owned: set[str], in_use: frozenset[str]
) -> str:
    if image in in_use:
        return "in-use"
    if any(not _owns_tag(identity, ref) for ref in refs):
        return "shared"
    return "unrecorded" if not refs and image not in owned else "unused"


def _images(identity: str) -> tuple[Leftover, ...]:
    """Images named by the Project's tags or owned-image records.

    A pull by digest can leave the Project's repository on an untagged image
    (`repo:<none>`); that image is listed as unrecorded. Untagged images with
    neither a record nor a repository can't be traced to any Project.
    """
    tags: dict[str, list[str]] = {}
    sizes: dict[str, str] = {}
    owned = set(OwnedImages(identity).ids)
    mine = set(owned)
    for row in _image_rows():
        sizes[row.image] = row.size
        if _is_tag(row.reference):
            tags.setdefault(row.image, []).append(row.reference)
        if _owns_tag(identity, row.reference):
            mine.add(row.image)
    in_use = interactive_docker.container_image_ids_strict() if mine else frozenset()
    result = []
    for image in sorted(mine & sizes.keys()):
        refs = tuple(sorted(tags.get(image, ())))
        reason = _image_reason(identity, image, refs, owned, in_use)
        result.append(Leftover("image", image, reason, sizes[image], refs))
    return tuple(result)


def _volume_sizes() -> dict[str, str]:
    """Docker computes volume sizes only in its slow disk-usage report; sizes are optional."""
    args = ["system", "df", "--verbose", "--format", "{{json .}}"]
    try:
        report = require_dict(json.loads("".join(_lines(args, timeout=60))))
        rows = require_list(report.get("Volumes") or [], field="Volumes")
        sizes = {
            require_str(row, "Name"): require_str(row, "Size") for row in map(require_dict, rows)
        }
    except (RuntimeError, ValueError):  # BoundaryError is a ValueError
        return {}
    return {name: size for name, size in sizes.items() if size != "N/A"}


def _volume_in_use(name: str) -> bool:
    return bool(_lines(["container", "ls", "--all", "--quiet", "--filter", f"volume={name}"]))


def _present_volumes(identity: str) -> list[str]:
    names = state_volume_names(project_name_id(identity))
    return sorted(names & set(_lines(["volume", "ls", "--format", "{{.Name}}"])))


def _volumes(identity: str) -> tuple[Leftover, ...]:
    present = _present_volumes(identity)
    sizes = _volume_sizes() if present else {}
    return tuple(
        Leftover("volume", name, "in-use" if _volume_in_use(name) else "unused", sizes.get(name))
        for name in present
    )


def _labelled(identity: str) -> tuple[Leftover, ...]:
    """Containers and networks (including license topology) still labelled for the Project."""
    result = []
    for kind, args, name_field in (
        ("container", ["container", "ls", "--all"], "{{.Names}}"),
        ("network", ["network", "ls"], "{{.Name}}"),
    ):
        names = {
            name
            for label in accepted_project_labels(identity)
            for name in _lines(
                [*args, "--filter", f"label=booley.project-id={label}", "--format", name_field]
            )
        }
        result.extend(Leftover(kind, name, "labelled") for name in sorted(names))
    return tuple(result)


def leftovers(identity: str) -> tuple[Leftover, ...]:
    """List every Booley-owned Docker resource that still carries this root's identity."""
    return (*_images(identity), *_volumes(identity), *_labelled(identity))


def _layer_count(image: str) -> int:
    return int("".join(_lines(["image", "inspect", "--format", "{{len .RootFS.Layers}}", image])))


def _release_own_tags(identity: str, removed: list[str]) -> set[str]:
    """Untag children before parents so Docker can reclaim each chain's bytes.

    Returns the IDs whose tags were released.
    """
    in_use = interactive_docker.container_image_ids_strict()
    tags = [
        (row.image, row.reference)
        for row in _image_rows()
        if _is_tag(row.reference)
        and _owns_tag(identity, row.reference)
        and row.image not in in_use
    ]
    layers = {image: _layer_count(image) for image, _reference in tags}
    for _image, reference in sorted(tags, key=lambda row: (-layers[row[0]], row[1])):
        # Non-force, no-prune: other tags keep the bytes, untagged parents stay.
        _lines(["image", "rm", "--no-prune", reference])
        removed.append(reference)
    return {image for image, _reference in tags}


def purge(identity: str) -> PurgeResult:
    """Remove the root's unused own tags, recorded untagged images and unused state volumes.

    Every removal is non-force. Unrecorded untagged images, labelled containers
    and networks, and anything a container uses stay for the caller to report.
    """
    removed: list[str] = []
    try:
        untagged = _release_own_tags(identity, removed)
        recorded = set(OwnedImages(identity).ids) & {row.image for row in _image_rows()}
        kept = list(cleanup_owned_images(identity))
        present = {row.image for row in _image_rows()}
        removed.extend(sorted(recorded - present))
        # Released bytes another image or tag still needs are no longer this Project's.
        kept.extend(
            f"{image}: bytes kept (another image or tag still needs them)"
            for image in sorted((untagged & present) - recorded)
        )
        for volume in _present_volumes(identity):
            if not _volume_in_use(volume):
                _lines(["volume", "rm", volume])
                removed.append(volume)
        if failed := [line for line in kept if ": deferred (" in line]:
            raise RuntimeError("could not remove recorded images: " + "; ".join(failed))
    except (RuntimeError, ValueError) as exc:  # ValueError: malformed Docker output
        done = ", ".join(removed) or "nothing"
        raise RuntimeError(f"{exc}; already removed: {done}; run the purge again") from exc
    return PurgeResult(tuple(removed), tuple(kept))


def own_tag_snapshot(identity: str, references: Iterable[ImageReference]) -> dict[str, str]:
    """The Project's managed tags (keeper excluded) and the image IDs they name."""
    keeper = keeper_image_for_identity(identity)
    return {
        row.reference: row.image_id
        for row in references
        if _is_tag(row.reference)
        and _owns_tag(identity, row.reference)
        and row.reference != keeper
    }


def record_superseded_images(
    identity: str, before: dict[str, str], after: dict[str, str]
) -> tuple[str, ...]:
    """Own the images an adoption moved off the Project's managed tags, for later cleanup.

    *before* and *after* are :func:`own_tag_snapshot` results around adoption;
    this covers moved tags and removed ones (such as an obsolete layout tag).
    Only the init path calls this: session refresh keeps its prior images for
    rollback. A tag outside the Project's family (a shared substrate) never
    makes its bytes owned.
    """
    current = set(after.values())
    moved = tuple(
        dict.fromkeys(
            image
            for reference, image in before.items()
            if after.get(reference) != image and image not in current
        )
    )
    if moved:
        OwnedImages(Path(identity)).record({}, moved)
    return moved
