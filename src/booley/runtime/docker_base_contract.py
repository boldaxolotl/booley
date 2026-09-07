#!/usr/bin/env python3
"""Fingerprint every declared input to the published runtime base image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DEFAULT_MANIFEST = "src/booley/data/docker/stable-base-inputs.txt"
_CONTRACT_LABEL = "io.booley.runtime-base.contract"
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_IMAGE_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}


def _repo_file(repo: Path, relative: str, purpose: str) -> Path:
    """Resolve a declared repository file without permitting path escapes."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe {purpose}: {relative}")
    unresolved = repo.joinpath(*pure.parts)
    try:
        path = unresolved.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"missing {purpose}: {relative}") from error
    if not path.is_relative_to(repo) or unresolved.is_symlink():
        raise ValueError(f"unsafe {purpose}: {relative}")
    if not path.is_file():
        raise ValueError(f"missing {purpose}: {relative}")
    return path


def input_paths(repo: Path, manifest: str = DEFAULT_MANIFEST) -> tuple[Path, ...]:
    repo = repo.resolve()
    manifest_path = _repo_file(repo, manifest, "stable-base manifest")
    paths: list[Path] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        relative = line.strip()
        if not relative or relative.startswith("#"):
            continue
        paths.append(_repo_file(repo, relative, "stable-base input"))
    if not paths:
        raise ValueError("stable-base input manifest is empty")
    return tuple(paths)


def stable_base_inputs(repo: Path, manifest: str = DEFAULT_MANIFEST) -> tuple[str, ...]:
    repo = repo.resolve()
    return tuple(path.relative_to(repo).as_posix() for path in input_paths(repo, manifest))


def contract(repo: Path, manifest: str = DEFAULT_MANIFEST) -> str:
    repo = repo.resolve()
    digest = hashlib.sha256()
    for path in input_paths(repo, manifest):
        relative = path.relative_to(repo).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        contents = path.read_bytes()
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _repository(reference: str) -> str:
    repository = reference.rsplit("@", 1)[0]
    last_slash = repository.rfind("/")
    last_colon = repository.rfind(":")
    return repository[:last_colon] if last_colon > last_slash else repository


def _require_digest(value: object, purpose: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {purpose} digest")
    return value


def _require_schema_v2(document: dict, purpose: str) -> None:
    if document.get("schemaVersion") != 2:
        raise ValueError(f"{purpose} requires schema version 2")


def _remote_document(reference: str, field: str) -> dict:
    inspected = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            f"{{{{json .{field}}}}}",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    try:
        document = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid remote {field} response for {reference}") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid remote {field} response for {reference}")
    return document


def _remote_raw_manifest(reference: str) -> dict:
    inspected = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference, "--raw"],
        capture_output=True,
        check=True,
        text=True,
    )
    try:
        manifest = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid remote raw manifest for {reference}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"invalid remote raw manifest for {reference}")
    return manifest


def _linux_amd64_descriptor(index: dict, reference: str) -> dict:
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ValueError(f"invalid OCI image index for {reference}")
    candidates = []
    for descriptor in manifests:
        if not isinstance(descriptor, dict):
            raise ValueError(f"invalid OCI image index for {reference}")
        annotations = descriptor.get("annotations", {})
        if isinstance(annotations, dict) and annotations.get("vnd.docker.reference.type") == (
            "attestation-manifest"
        ):
            continue
        platform = descriptor.get("platform")
        if (
            isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") == "amd64"
        ):
            if descriptor.get("mediaType") not in _IMAGE_MANIFEST_MEDIA_TYPES:
                raise ValueError(
                    f"Linux/AMD64 descriptor is not an image manifest for {reference}"
                )
            candidates.append(descriptor)
    if len(candidates) != 1:
        raise ValueError(f"expected one Linux/AMD64 image in OCI index for {reference}")
    return candidates[0]


def _verify_remote_image_config(image: dict, expected_contract: str, reference: str) -> None:
    if image.get("os") != "linux" or image.get("architecture") != "amd64":
        raise ValueError(f"remote image configuration is not Linux/AMD64: {reference}")
    config = image.get("config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    label = labels.get(_CONTRACT_LABEL) if isinstance(labels, dict) else None
    if label != expected_contract:
        raise ValueError(
            f"stable-base contract mismatch: expected {expected_contract}, "
            f"image has {label or '<none>'}"
        )


def _is_attestation_manifest(manifest: dict) -> bool:
    annotations = manifest.get("annotations")
    reference_type = (
        annotations.get("vnd.docker.reference.type") if isinstance(annotations, dict) else None
    )
    return manifest.get("artifactType") is not None or reference_type == "attestation-manifest"


def resolve_image_remote(reference: str, expected_contract: str) -> str:
    """Resolve and validate a registry image without pulling its layers."""
    repository = _repository(reference)
    top = _remote_document(reference, "Manifest")
    if "schemaVersion" in top:
        _require_schema_v2(top, f"OCI index or image manifest for {reference}")
    top_digest = _require_digest(top.get("digest"), "OCI index or image manifest")
    media_type = top.get("mediaType")
    if media_type in _IMAGE_INDEX_MEDIA_TYPES:
        descriptor = _linux_amd64_descriptor(top, reference)
        image_digest = _require_digest(descriptor.get("digest"), "Linux/AMD64 image manifest")
        image_reference = f"{repository}@{image_digest}"
    elif media_type in _IMAGE_MANIFEST_MEDIA_TYPES:
        image_digest = top_digest
        image_reference = f"{repository}@{image_digest}"
    else:
        raise ValueError(f"unsupported OCI media type for {reference}: {media_type}")
    manifest = _remote_raw_manifest(image_reference)
    _require_schema_v2(manifest, f"image manifest for {image_reference}")
    if manifest.get("mediaType") not in _IMAGE_MANIFEST_MEDIA_TYPES:
        raise ValueError(f"selected descriptor is not an image manifest: {image_reference}")
    if _is_attestation_manifest(manifest):
        raise ValueError(f"selected manifest is an attestation: {image_reference}")
    config = manifest.get("config")
    if not isinstance(config, dict) or config.get("mediaType") not in _IMAGE_CONFIG_MEDIA_TYPES:
        raise ValueError(f"invalid image configuration descriptor: {image_reference}")
    _require_digest(config.get("digest"), "image configuration")
    image = _remote_document(image_reference, "Image")
    _verify_remote_image_config(image, expected_contract, image_reference)
    return f"{repository}@{top_digest}"


def resolve_image_pull(reference: str, expected_contract: str) -> str:
    """Resolve and validate an image by pulling it into the Docker daemon."""
    subprocess.run(["docker", "pull", reference], check=True)
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{ json .RepoDigests }}", reference],
        capture_output=True,
        check=True,
        text=True,
    )
    digests = json.loads(inspected.stdout)
    if (
        not isinstance(digests, list)
        or not digests
        or not all(isinstance(value, str) for value in digests)
    ):
        raise ValueError(f"invalid Docker RepoDigests response for {reference}")
    repository = _repository(reference)
    digest_pattern = re.compile(rf"{re.escape(repository)}@sha256:[0-9a-f]{{64}}")
    immutable = next((value for value in digests if digest_pattern.fullmatch(value)), None)
    if immutable is None:
        raise ValueError(f"image did not resolve to an immutable digest: {reference}")
    label = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "io.booley.runtime-base.contract" }}',
            immutable,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if label != expected_contract:
        raise ValueError(
            f"stable-base contract mismatch: expected {expected_contract}, image has {label or '<none>'}"
        )
    return immutable


def resolve_image(reference: str, expected_contract: str) -> str:
    """Shadow-compare remote metadata resolution with the pull-based result."""
    remote = resolve_image_remote(reference, expected_contract)
    pulled = resolve_image_pull(reference, expected_contract)
    if remote != pulled:
        raise ValueError(
            f"remote and pull resolvers disagree for {reference}: remote {remote}, pull {pulled}"
        )
    return remote


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--resolve-image")
    parser.add_argument("--resolver", choices=("shadow", "remote", "pull"), default="shadow")
    args = parser.parse_args()
    try:
        value = contract(args.repo.resolve(), args.manifest)
        resolvers = {
            "shadow": resolve_image,
            "remote": resolve_image_remote,
            "pull": resolve_image_pull,
        }
        image = resolvers[args.resolver](args.resolve_image, value) if args.resolve_image else None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            print(f"contract={value}", file=stream)
            if image:
                print(f"image={image}", file=stream)
    print(image or value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
