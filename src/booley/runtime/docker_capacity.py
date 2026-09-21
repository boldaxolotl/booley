"""Fail fast when Docker image builds would leave unsafe disk pressure."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

GIB = 2**30
COLD_BUILD_HEADROOM = 30 * GIB
CACHED_BUILD_HEADROOM = 10 * GIB
SAFETY_RESERVE = 5 * GIB
SKIP_PREFLIGHT_ENV = "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT"
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(i?)B", re.IGNORECASE)


class DockerCapacityError(OSError):
    """A Docker image build is predictably unsafe on the observed storage."""


@dataclass(frozen=True, slots=True)
class _BuildCache:
    total: int = 0
    reclaimable: int = 0


def _is_docker_build(command: Sequence[str]) -> bool:
    if len(command) < 2 or Path(command[0]).name != "docker":
        return False
    return command[1] == "build" or list(command[1:3]) == ["buildx", "build"]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
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
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


def _docker_storage(docker: str) -> Path | None:
    result = _run([docker, "info", "--format", "{{.DockerRootDir}}"])
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.strip()
    path = Path(value)
    return path if value and path.is_absolute() else None


def _target_is_cached(docker: str, image: str) -> bool:
    result = _run([docker, "image", "inspect", image, "--format", "{{.Id}}"])
    return bool(result and result.returncode == 0 and result.stdout.strip())


def _size_bytes(value: str) -> int:
    match = _SIZE.match(value)
    if not match:
        return 0
    scale = 1024 if match.group(3) else 1000
    powers = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}
    return int(float(match.group(1)) * scale ** powers[match.group(2).upper()])


def _build_cache(docker: str) -> _BuildCache:
    result = _run(
        [docker, "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"]
    )
    if result is None or result.returncode != 0:
        return _BuildCache()
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 3 and parts[0].lower() == "build cache":
            return _BuildCache(_size_bytes(parts[1]), _size_bytes(parts[2]))
    return _BuildCache()


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def _failure_message(
    image: str, storage: Path, available: int, headroom: int, cache: _BuildCache
) -> str:
    cache_state = "cached-target" if headroom == CACHED_BUILD_HEADROOM else "cold-build"
    usage = ""
    if cache.total:
        usage = (
            f" Docker build cache uses {_gib(cache.total)}; "
            f"{_gib(cache.reclaimable)} reclaimable."
        )
    return (
        f"Insufficient disk capacity to build {image}: {_gib(available)} available on "
        f"Docker storage at {storage}, but {_gib(headroom + SAFETY_RESERVE)} required "
        f"({_gib(headroom)} {cache_state} headroom + {_gib(SAFETY_RESERVE)} safety "
        f"reserve).{usage} Free unused build cache with `docker builder prune`; Booley "
        "will not delete images, volumes, Project artifacts, or user data. Retry after "
        f"cleanup, or bypass once with {SKIP_PREFLIGHT_ENV}=1 when Docker storage is "
        "reported elsewhere."
    )


def ensure_docker_build_capacity(command: Sequence[str], *, image: str) -> None:
    """Reject a Docker build that cannot preserve conservative free headroom."""
    if not _is_docker_build(command) or os.environ.get(SKIP_PREFLIGHT_ENV) == "1":
        return
    storage = _docker_storage(command[0])
    if storage is None:
        return
    try:
        available = shutil.disk_usage(storage).free
    except OSError:
        return
    cache = _build_cache(command[0])
    cached = cache.total > 0 and _target_is_cached(command[0], image)
    headroom = CACHED_BUILD_HEADROOM if cached else COLD_BUILD_HEADROOM
    if available < headroom + SAFETY_RESERVE:
        raise DockerCapacityError(
            _failure_message(image, storage, available, headroom, cache)
        )
