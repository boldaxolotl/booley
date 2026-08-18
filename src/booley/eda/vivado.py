"""Built-in host-provisioned Vivado policy for Linux x86-64."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.paths import package_data_dir

KIND = "vivado"
POLICY_REVISION = 1
CONTAINER_TARGET = "/opt/booley-eda/vivado"
WRAPPER_TARGET = "/usr/local/bin/vivado"
SUPPORTED_VERSION = "2025.2"
SUPPORTED_ARCHITECTURE = "linux-x86_64"
_VERSION_RE = re.compile(r"\bVivado(?:\s+Design\s+Suite)?\s+v?(\d{4}\.\d+)\b", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class VivadoPolicyError(RuntimeError):
    """A Vivado installation does not satisfy the built-in policy."""


@dataclass(frozen=True)
class Inspection:
    """Observed installation identity persisted by host registration."""

    source: Path
    version: str
    architecture: str


def wrapper_path() -> Path:
    """Return the Booley-owned wrapper installed into the standard image."""
    return package_data_dir() / "docker" / "vivado-wrapper"


def wrapper_sha256() -> str:
    """Return the expected wrapper digest for spec and Doctor checks."""
    try:
        return hashlib.sha256(wrapper_path().read_bytes()).hexdigest()
    except OSError as exc:
        raise VivadoPolicyError(f"cannot read the packaged Vivado wrapper: {exc}") from exc


def inspect_installation(source: Path, *, project_root: Path | None = None) -> Inspection:
    """Canonicalize, validate, and identify one Xilinx release root."""
    root = _canonical_source(source)
    if project_root is not None:
        project = project_root.resolve(strict=True)
        if _overlaps(root, project):
            raise VivadoPolicyError("Vivado installation overlaps the Project root")
    launcher = root / "Vivado" / "bin" / "vivado"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise VivadoPolicyError("release root must contain executable Vivado/bin/vivado")
    if not (root / "tps").is_dir():
        raise VivadoPolicyError("release root must contain the sibling tps/ directory")
    architecture = _host_architecture()
    version = _detect_version(launcher)
    _validate_supported(version, architecture)
    return Inspection(root, version, architecture)


def _canonical_source(source: Path) -> Path:
    raw = str(source)
    if not source.is_absolute() or "," in raw or _CONTROL_RE.search(raw):
        raise VivadoPolicyError("Vivado source must be a safe absolute path")
    try:
        root = source.resolve(strict=True)
    except OSError as exc:
        raise VivadoPolicyError(f"Vivado source is unavailable: {source} ({exc})") from exc
    if not root.is_dir():
        raise VivadoPolicyError(f"Vivado source is not a directory: {root}")
    home = Path.home().resolve()
    anchor = Path(root.anchor)
    if root in {anchor, home} or root.parent == anchor:
        raise VivadoPolicyError(f"Vivado source is an unsafe broad root: {root}")
    return root


def _host_architecture() -> str:
    machine = platform.machine().lower()
    if not sys.platform.startswith("linux") or machine not in {"x86_64", "amd64"}:
        raise VivadoPolicyError(
            "host-provisioned Vivado currently supports Linux x86-64 only; "
            "Windows, macOS, and other architectures are future work"
        )
    return SUPPORTED_ARCHITECTURE


def _detect_version(launcher: Path) -> str:
    try:
        result = subprocess.run(
            [str(launcher), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VivadoPolicyError(f"could not execute Vivado version probe: {exc}") from exc
    combined = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_RE.search(combined)
    if result.returncode != 0 or match is None:
        raise VivadoPolicyError("Vivado version probe did not report a supported version")
    return match.group(1)


def _validate_supported(version: str, architecture: str) -> None:
    if version != SUPPORTED_VERSION:
        raise VivadoPolicyError(
            f"Vivado {version} is not validated; this policy supports {SUPPORTED_VERSION} only"
        )
    if architecture != SUPPORTED_ARCHITECTURE:
        raise VivadoPolicyError(f"unsupported Vivado architecture: {architecture}")


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
