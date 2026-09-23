"""Compatibility identities for repository-built Sandbox Image layers."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from booley.runtime import docker_base_contract
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

_STANDARD_SUBSTRATE_INPUTS = (
    "crates/bwave/Cargo.toml",
    "crates/bwave/Cargo.lock",
    "crates/bwave/src",
    "crates/bwave/schema",
    "crates/bwave/docs",
    "src/booley/data/edalize/verible.py",
)


class ImageBuildContractMetadataError(ValueError):
    """An installed distribution has unusable Sandbox Image contract metadata."""


@dataclass(frozen=True, slots=True)
class ImageBuildContracts:
    """Compatibility identities for the repository-owned reusable layers."""

    runtime_base: str
    standard_substrate: str


def _standard_input_files(root: Path) -> tuple[Path, ...]:
    files: set[Path] = set()
    resolved_root = root.resolve()
    for relative in _STANDARD_SUBSTRATE_INPUTS:
        declared = root / relative
        if declared.is_symlink() or not declared.exists():
            raise ValueError(f"missing or unsafe standard-substrate input: {relative}")
        if declared.is_dir():
            members = tuple(item for item in declared.rglob("*") if item.is_file())
            if not members:
                raise ValueError(f"empty standard-substrate input directory: {relative}")
            candidates = members
        elif declared.is_file():
            candidates = (declared,)
        else:
            raise ValueError(f"missing standard-substrate input: {relative}")
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValueError(
                    f"unsafe standard-substrate input: {candidate.relative_to(root).as_posix()}"
                )
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                raise ValueError(
                    f"unsafe standard-substrate input: {candidate.relative_to(root).as_posix()}"
                )
            files.add(candidate)
    if not files:
        raise ValueError("standard-substrate inputs are empty")
    return tuple(sorted(files))


def standard_substrate_contract(root: Path) -> str:
    """Return the exact source identity of the standard tool substrate."""
    digest = hashlib.sha256()
    for path in _standard_input_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_image_build_contracts(root: Path) -> ImageBuildContracts:
    """Calculate image contracts from an attributed source checkout."""
    return ImageBuildContracts(
        runtime_base=docker_base_contract.contract(root),
        standard_substrate=standard_substrate_contract(root),
    )


def _canonical_sha256(value: object, name: str) -> str:
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        raise ImageBuildContractMetadataError(
            f"installed Booley distribution has invalid {name} compatibility metadata"
        )
    return value


def embedded_image_build_contracts() -> ImageBuildContracts:
    """Read and validate contracts stamped into an installed distribution."""
    try:
        source = _embedded_stamp_path().read_text(encoding="utf-8")
        module = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        raise ImageBuildContractMetadataError(
            "installed Booley distribution lacks Sandbox Image compatibility metadata"
        ) from exc
    values: dict[str, object] = {}
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {
            "RUNTIME_BASE_CONTRACT",
            "STANDARD_SUBSTRATE_CONTRACT",
        }:
            continue
        try:
            values[target.id] = ast.literal_eval(statement.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ImageBuildContractMetadataError(
                f"installed Booley distribution has invalid {target.id.lower()} metadata"
            ) from exc
    return ImageBuildContracts(
        runtime_base=_canonical_sha256(values.get("RUNTIME_BASE_CONTRACT"), "runtime-base"),
        standard_substrate=_canonical_sha256(
            values.get("STANDARD_SUBSTRATE_CONTRACT"), "standard-substrate"
        ),
    )


def _embedded_stamp_path() -> Path:
    """Return the generated provenance module beside the installed package."""
    return Path(__file__).resolve().parents[1] / "_build_commit.py"


def expected_image_build_contracts(
    attribution: VersionAttribution,
) -> ImageBuildContracts:
    """Resolve contracts from the running Booley artifact's authoritative origin."""
    if attribution.origin is VersionOrigin.SOURCE:
        assert attribution.source_root is not None
        return source_image_build_contracts(attribution.source_root)
    if attribution.origin is VersionOrigin.DISTRIBUTION:
        return embedded_image_build_contracts()
    raise ImageBuildContractMetadataError(
        "running Booley code has no attributable source for Sandbox Image metadata"
    )
