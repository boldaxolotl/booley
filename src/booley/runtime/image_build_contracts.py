"""Compatibility identities for repository-built Sandbox Image layers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.runtime import docker_base_contract
from booley.runtime.build_stamp import files_fingerprint, stamp_constants
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

# The B-Wave crate entries stay here even though the binary is now built in the
# wheel overlay.  The overlay's identity is the wheel-source fingerprint (which
# covers only src/booley, pyproject.toml and VERSION) plus its parent
# substrate's compatibility key, so dropping the crate inputs would let a crate
# edit reuse a stale overlay binary.
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
    return files_fingerprint(root, _standard_input_files(root))


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
    except (OSError, UnicodeDecodeError) as exc:
        raise ImageBuildContractMetadataError(
            "installed Booley distribution lacks Sandbox Image compatibility metadata"
        ) from exc
    try:
        values = stamp_constants(source, {"RUNTIME_BASE_CONTRACT", "STANDARD_SUBSTRATE_CONTRACT"})
    except ValueError as exc:
        raise ImageBuildContractMetadataError(
            f"installed Booley distribution has invalid Sandbox Image compatibility metadata: {exc}"
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
