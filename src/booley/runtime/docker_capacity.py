"""Fail fast when Docker image builds would leave unsafe disk pressure."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

GIB = 2**30
COLD_BUILD_HEADROOM = 30 * GIB
CACHED_BUILD_HEADROOM = 10 * GIB
THIN_BUILD_HEADROOM = 5 * GIB
HEAVY_RETAINED_GROWTH = 5 * GIB
THIN_RETAINED_GROWTH = 2 * GIB
SAFETY_RESERVE = 5 * GIB
SKIP_PREFLIGHT_ENV = "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT"
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(i?)B", re.IGNORECASE)


class DockerCapacityError(OSError):
    """A Docker image build is predictably unsafe on the observed storage."""


class BuildEstimateClass(StrEnum):
    """Conservative capacity class for one Docker build recipe.

    Retained growth is bounded separately from the temporary peak so a sequence
    does not sum several mutually exclusive peak allowances. Temporary peaks
    preserve the previous conservative single-build bounds.
    """

    HEAVYWEIGHT = "heavyweight"
    THIN_OVERLAY = "thin-overlay"


@dataclass(frozen=True, slots=True)
class DockerCacheEvidence:
    """Stable managed image and exact labels required to claim warm inputs."""

    reference: str
    expected_labels: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DockerBuildRequest:
    """Capacity facts for one planned Docker build."""

    managed_image: str
    output_tag: str
    estimate_class: BuildEstimateClass = BuildEstimateClass.HEAVYWEIGHT
    cache_evidence: DockerCacheEvidence | None = None


@dataclass(frozen=True, slots=True)
class DockerBuildPlan:
    """An immutable ordered description of the currently known build tail."""

    requests: tuple[DockerBuildRequest, ...]

    def __post_init__(self) -> None:
        if not self.requests:
            raise ValueError("a Docker build plan must contain at least one request")


@dataclass(frozen=True, slots=True)
class _BuildCache:
    total: int = 0
    reclaimable: int = 0


def _is_docker_build(command: Sequence[str]) -> bool:
    if len(command) < 2 or Path(command[0]).stem.lower() != "docker":
        return False
    return command[1] == "build" or list(command[1:3]) == ["buildx", "build"]


def _run_docker_probe(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded Docker inspection command or fail the preflight."""
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise DockerCapacityError(
            f"could not run Docker capacity probe {' '.join(command[1:])}: {exc}"
        ) from exc


def _probe_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"Docker exited {result.returncode}"


def _docker_storage(docker: str) -> Path:
    result = _run_docker_probe([docker, "info", "--format", "{{.DockerRootDir}}"])
    if result.returncode != 0:
        raise DockerCapacityError(f"could not query Docker storage root: {_probe_detail(result)}")
    value = result.stdout.strip()
    path = Path(value)
    if not value or not path.is_absolute():
        raise DockerCapacityError(f"Docker reported an invalid storage root: {value or '<empty>'}")
    return path


def _target_is_cached(docker: str, image: str) -> bool:
    result = _run_docker_probe([docker, "image", "inspect", image, "--format", "{{.Id}}"])
    if result.returncode == 0 and result.stdout.strip():
        return True
    detail = _probe_detail(result).lower()
    if "no such image" in detail or "not found" in detail:
        return False
    raise DockerCapacityError(
        f"could not inspect target Docker image {image}: {_probe_detail(result)}"
    )


def _image_label(docker: str, image: str, label: str) -> str | None:
    result = _run_docker_probe(
        [
            docker,
            "image",
            "inspect",
            image,
            "--format",
            f'{{{{ index .Config.Labels "{label}" }}}}',
        ]
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    detail = _probe_detail(result).lower()
    if "no such image" in detail or "not found" in detail:
        return None
    raise DockerCapacityError(
        f"could not inspect cache evidence Docker image {image}: {_probe_detail(result)}"
    )


def _has_warm_evidence(docker: str, request: DockerBuildRequest, cache: _BuildCache) -> bool:
    evidence = request.cache_evidence
    if cache.total <= 0 or evidence is None or not evidence.expected_labels:
        return False
    return all(
        _image_label(docker, evidence.reference, name) == expected
        for name, expected in evidence.expected_labels
    )


def _size_bytes(value: str) -> int:
    match = _SIZE.match(value)
    if not match:
        raise DockerCapacityError(f"Docker reported an invalid size: {value!r}")
    scale = 1024 if match.group(3) else 1000
    powers = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}
    return int(float(match.group(1)) * scale ** powers[match.group(2).upper()])


def _build_cache(docker: str) -> _BuildCache:
    result = _run_docker_probe(
        [docker, "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"]
    )
    if result.returncode != 0:
        raise DockerCapacityError(f"could not query Docker build cache: {_probe_detail(result)}")
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 3 and parts[0].lower() == "build cache":
            return _BuildCache(_size_bytes(parts[1]), _size_bytes(parts[2]))
    raise DockerCapacityError("Docker did not report build-cache usage")


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def _estimate(request: DockerBuildRequest, *, warm: bool) -> tuple[int, int, str]:
    if request.estimate_class is BuildEstimateClass.THIN_OVERLAY:
        return THIN_BUILD_HEADROOM, THIN_RETAINED_GROWTH, "thin"
    if warm:
        return CACHED_BUILD_HEADROOM, HEAVY_RETAINED_GROWTH, "warm"
    return COLD_BUILD_HEADROOM, HEAVY_RETAINED_GROWTH, "cold"


def required_sequence_headroom(
    plan: DockerBuildPlan, *, warm: Sequence[bool] | None = None
) -> int:
    """Return retained-prefix plus peak headroom, with one safety reserve."""
    warm_states = tuple(False for _ in plan.requests) if warm is None else tuple(warm)
    if len(warm_states) != len(plan.requests):
        raise ValueError("warm state count must match the build plan")
    retained = 0
    peak = 0
    for request, is_warm in zip(plan.requests, warm_states, strict=True):
        temporary, growth, _ = _estimate(request, warm=is_warm)
        peak = max(peak, retained + temporary)
        retained += growth
    return peak + SAFETY_RESERVE


def _failure_message(
    plan: DockerBuildPlan,
    storage: Path,
    available: int,
    *,
    warm: Sequence[bool],
    cache: _BuildCache,
) -> str:
    required = required_sequence_headroom(plan, warm=warm)
    names = " -> ".join(request.managed_image for request in plan.requests)
    breakdown = []
    for request, is_warm in zip(plan.requests, warm, strict=True):
        temporary, growth, state = _estimate(request, warm=is_warm)
        breakdown.append(
            f"{request.managed_image}: {_gib(temporary)} {state} temporary peak, "
            f"{_gib(growth)} retained growth"
        )
    usage = f" Docker build cache uses {_gib(cache.total)}; {_gib(cache.reclaimable)} reclaimable."
    return (
        f"Insufficient disk capacity for planned Docker builds {names}: {_gib(available)} "
        f"available on Docker storage at {storage}, but {_gib(required)} required "
        f"({'; '.join(breakdown)}; {_gib(SAFETY_RESERVE)} safety reserve).{usage} "
        "If you free unused build cache with `docker builder prune`, note that pruning may "
        "evict layers this sequence would otherwise reuse. Booley will not delete cache, "
        "images, volumes, Project artifacts, or user data. Recheck the complete sequence "
        f"after cleanup, or bypass once with {SKIP_PREFLIGHT_ENV}=1 when Docker storage "
        "is reported elsewhere."
    )


def ensure_docker_build_capacity(
    command: Sequence[str],
    *,
    image: str | None = None,
    current_request: DockerBuildRequest | None = None,
    remaining_plan: DockerBuildPlan | None = None,
) -> None:
    """Reject a Docker build that cannot preserve conservative free headroom."""
    if not _is_docker_build(command) or os.environ.get(SKIP_PREFLIGHT_ENV) == "1":
        return
    if current_request is None:
        if image is None:
            raise ValueError("image or current_request is required")
        current_request = DockerBuildRequest(image, image)
    if remaining_plan is None:
        remaining_plan = DockerBuildPlan((current_request,))
    if remaining_plan.requests[0] != current_request:
        raise ValueError("the current Docker build must be first in the remaining plan")
    if image is not None and image != current_request.output_tag:
        raise ValueError("the Docker build image must match the current request output tag")
    storage = _docker_storage(command[0])
    try:
        available = shutil.disk_usage(storage).free
    except OSError as exc:
        raise DockerCapacityError(f"could not inspect Docker storage at {storage}: {exc}") from exc
    cache = _build_cache(command[0])
    warm = tuple(
        _has_warm_evidence(command[0], request, cache) for request in remaining_plan.requests
    )
    if available < required_sequence_headroom(remaining_plan, warm=warm):
        raise DockerCapacityError(
            _failure_message(remaining_plan, storage, available, warm=warm, cache=cache)
        )


def _request_from_document(entry: object) -> DockerBuildRequest:
    if not isinstance(entry, dict):
        raise TypeError("each request must be an object")
    managed_image = entry.get("managed_image")
    output_tag = entry.get("output_tag")
    estimate_class = entry.get("estimate_class")
    if not isinstance(managed_image, str) or not managed_image.strip():
        raise TypeError("managed_image must be a non-empty string")
    if not isinstance(output_tag, str) or not output_tag.strip():
        raise TypeError("output_tag must be a non-empty string")
    if not isinstance(estimate_class, str):
        raise TypeError("estimate_class must be a string")
    return DockerBuildRequest(
        managed_image,
        output_tag,
        BuildEstimateClass(estimate_class),
    )


def _plan_from_file(path: Path, current_index: int) -> tuple[DockerBuildRequest, DockerBuildPlan]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("requests"), list):
            raise TypeError("root must contain a requests list")
        if current_index < 0:
            raise ValueError("current index must not be negative")
        requests = tuple(
            _request_from_document(entry) for entry in payload["requests"][current_index:]
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DockerCapacityError(f"invalid Docker build plan file {path}: {exc}") from exc
    if not requests:
        raise DockerCapacityError("Docker build plan has no request at the current index")
    return requests[0], DockerBuildPlan(requests)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the preflight for a shell-script-provided Docker command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--current-index", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = tuple(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a Docker build command is required")
    try:
        current = plan = None
        if args.plan_file is not None:
            current, plan = _plan_from_file(args.plan_file, args.current_index)
            if current.output_tag != args.image:
                raise DockerCapacityError("current plan output tag does not match --image")
        ensure_docker_build_capacity(
            command,
            image=args.image,
            current_request=current,
            remaining_plan=plan,
        )
    except DockerCapacityError as exc:
        print(f"Docker image-build preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
