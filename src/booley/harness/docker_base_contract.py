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


def resolve_image(reference: str, expected_contract: str) -> str:
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
    repository = reference.rsplit("@", 1)[0]
    last_slash = repository.rfind("/")
    last_colon = repository.rfind(":")
    if last_colon > last_slash:
        repository = repository[:last_colon]
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--resolve-image")
    args = parser.parse_args()
    try:
        value = contract(args.repo.resolve(), args.manifest)
        image = resolve_image(args.resolve_image, value) if args.resolve_image else None
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
