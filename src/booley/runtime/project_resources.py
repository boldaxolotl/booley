"""Docker resources Booley creates for one Project, found by its canonical root.

Every name derives from the stored root string, so a deleted or replaced
directory still maps to the resources it created. Ownership of untagged image
bytes comes only from the image lifecycle's owned-image records.
"""

from __future__ import annotations

import json
import subprocess

from booley.config.sandbox import project_image_name_for_identity
from booley.core.project_identity import accepted_project_labels, project_name_id
from booley.projects.image_keepers import Leftover
from booley.runtime import interactive_docker
from booley.runtime.devcontainer import state_volume_names
from booley.runtime.image_lifecycle import owned_image_ids
from booley.runtime.session_issuance import keeper_image_for_identity

# Docker prints a missing repository or tag as this placeholder.
_UNTAGGED = "<none>"


def _docker(args: list[str], *, timeout: int = 30) -> str:
    try:
        result = interactive_docker._run_docker(args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Docker command failed"
        raise RuntimeError(f"docker {' '.join(args[:2])}: {detail}")
    return result.stdout


def _lines(args: list[str]) -> list[str]:
    return [line for line in _docker(args).splitlines() if line]


def owns_tag(identity: str, reference: str) -> bool:
    """Whether a tag's repository is this root's keeper or managed image family."""
    family = project_image_name_for_identity(identity)
    repository = reference.rpartition(":")[0]
    return (
        repository == keeper_image_for_identity(identity).rpartition(":")[0]
        or repository == family
        or repository.startswith(family + "-")
    )


def _image_rows() -> list[tuple[str, str, str]]:
    rows = []
    for row in _lines(
        [
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{.ID}}\t{{.Repository}}:{{.Tag}}\t{{.Size}}",
        ]
    ):
        image, reference, size = row.split("\t")
        rows.append((interactive_docker.validated_image_id(image), reference, size))
    return rows


def _images(identity: str) -> tuple[Leftover, ...]:
    tags: dict[str, list[str]] = {}
    sizes: dict[str, str] = {}
    owned = set(owned_image_ids(identity))
    mine = set(owned)
    for image, reference, size in _image_rows():
        sizes[image] = size
        repository, _, tag = reference.rpartition(":")
        if _UNTAGGED not in (repository, tag):
            tags.setdefault(image, []).append(reference)
        # A dangling image keeps the repository of the Project tag it lost.
        if owns_tag(identity, reference):
            mine.add(image)
    in_use = interactive_docker.container_image_ids_strict() if mine else frozenset()
    result = []
    for image in sorted(mine & sizes.keys()):
        refs = tuple(sorted(tags.get(image, ())))
        if image in in_use:
            reason = "in-use"
        elif any(not owns_tag(identity, ref) for ref in refs):
            reason = "shared"
        elif not refs and image not in owned:
            reason = "unrecorded"
        else:
            reason = "unused"
        result.append(Leftover("image", image, reason, sizes[image], refs))
    return tuple(result)


def _volume_sizes() -> dict[str, str]:
    """Docker computes volume sizes only in its slow disk-usage report; sizes are optional."""
    try:
        report = json.loads(
            _docker(["system", "df", "--verbose", "--format", "{{json .}}"], timeout=60)
        )
        return {row["Name"]: row["Size"] for row in report.get("Volumes") or ()}
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def _volumes(identity: str) -> tuple[Leftover, ...]:
    names = state_volume_names(project_name_id(identity))
    present = sorted(names & set(_lines(["volume", "ls", "--format", "{{.Name}}"])))
    sizes = _volume_sizes() if present else {}
    return tuple(
        Leftover(
            "volume",
            name,
            "in-use"
            if _lines(["container", "ls", "--all", "--quiet", "--filter", f"volume={name}"])
            else "unused",
            sizes.get(name),
        )
        for name in present
    )


def _labelled(identity: str) -> tuple[Leftover, ...]:
    """Containers and networks (including license topology) still labelled for the Project."""
    result = []
    for kind, args in (
        ("container", ["container", "ls", "--all"]),
        ("network", ["network", "ls"]),
    ):
        names = set()
        for label in accepted_project_labels(identity):
            names.update(
                _lines(
                    [
                        *args,
                        "--filter",
                        f"label=booley.project-id={label}",
                        "--format",
                        "{{.Name}}" if kind == "network" else "{{.Names}}",
                    ]
                )
            )
        result.extend(Leftover(kind, name, "labelled") for name in sorted(names))
    return tuple(result)


def leftovers(identity: str) -> tuple[Leftover, ...]:
    """List every Booley-owned Docker resource that still carries this root's identity."""
    return (*_images(identity), *_volumes(identity), *_labelled(identity))
