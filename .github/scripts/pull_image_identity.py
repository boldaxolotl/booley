#!/usr/bin/env python3
"""Pull a tagged Docker image and retain its immutable registry identity."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.core.boundary import is_str_list, require_dict, require_list, require_str

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ImageIdentity:
    requested_reference: str
    resolved_reference: str
    image_id: str
    repository_digests: tuple[str, ...]


def repository(reference: str) -> str:
    """Return the repository portion of a tag or digest reference."""
    without_digest = reference.partition("@")[0]
    tail = without_digest.rpartition("/")[2]
    if ":" in tail:
        return without_digest.rsplit(":", 1)[0]
    return without_digest


def image_identity(reference: str, document: object) -> ImageIdentity:
    """Select the unique immutable identity for ``reference`` from inspect JSON."""
    rows = require_list(document, field=f"Docker inspect rows for {reference!r}")
    if len(rows) != 1:
        raise ValueError(f"Docker returned {len(rows)} inspect rows for {reference!r}")
    inspected = require_dict(rows[0], field=f"Docker inspect row for {reference!r}")
    image_id = require_str(inspected, "Id")
    digests = inspected.get("RepoDigests")
    if _DIGEST.fullmatch(image_id) is None:
        raise ValueError(f"Docker returned an invalid image ID for {reference!r}")
    if not is_str_list(digests):
        raise ValueError(f"Docker returned invalid RepoDigests for {reference!r}")
    prefix = f"{repository(reference)}@"
    matches = sorted(
        {
            item
            for item in digests
            if item.startswith(prefix) and _DIGEST.fullmatch(item[len(prefix) :])
        }
    )
    if len(matches) != 1:
        raise ValueError(f"Docker resolved {reference!r} to {len(matches)} matching digests")
    return ImageIdentity(reference, matches[0], image_id, tuple(sorted(digests)))


def pull_image_identity(reference: str) -> ImageIdentity:
    """Pull ``reference`` and return its verified immutable local identity."""
    subprocess.run(["docker", "pull", reference], check=True, timeout=1800)
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return image_identity(reference, json.loads(result.stdout))


def _write_evidence(path: Path, identity: ImageIdentity) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(asdict(identity), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args()

    identity = pull_image_identity(args.image)
    _write_evidence(args.evidence, identity)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"image={identity.resolved_reference}\n")
    print(identity.resolved_reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
