#!/usr/bin/env python3
"""Export the embedded runtime-base package inventory from one exact image.

The image is inspected once and every later Docker call uses its immutable
local image ID. The inventory is copied from a never-started container as a
bounded ``docker cp`` tar stream that is parsed in memory: exactly one regular
file member is accepted and nothing is extracted to, or read from, host paths
named by the archive. The accepted bytes are exported unchanged beside an
evidence record that binds them to the image identity.

This is a Linux CI utility: executable launch and pipe selection require POSIX.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import selectors
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

_HELPER = Path(__file__).parents[2] / "src/booley/data/docker/base_package_inventory.py"
DOCKER_TIMEOUT_SECONDS = 120
COPY_TIMEOUT_SECONDS = 120
VERIFY_TIMEOUT_SECONDS = 300
MAX_ARCHIVE_BYTES = 9 * 1024 * 1024
_READ_CHUNK = 64 * 1024


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("base_package_inventory", _HELPER)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inventory = _load_helper()


class ExportError(Exception):
    """The image inventory could not be exported or failed a gate."""


def _docker(argv: list[str], *, timeout: int = DOCKER_TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(
            ["docker", *argv], capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise ExportError(f"docker {argv[0]} timed out after {timeout}s") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ExportError(f"docker {' '.join(argv[:2])} failed: {detail}")
    return result.stdout


def inspect_image(reference: str) -> dict[str, Any]:
    """Return the immutable identity and labels of one local image."""
    rows = json.loads(_docker(["image", "inspect", reference]))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ExportError(f"expected one inspect row for {reference!r}")
    row = rows[0]
    config = row.get("Config") if isinstance(row.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    image_id = row.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ExportError(f"image {reference!r} has no immutable ID")
    digests = row.get("RepoDigests") if isinstance(row.get("RepoDigests"), list) else []
    return {
        "image_id": image_id,
        "repo_digests": sorted(str(value) for value in digests),
        "label": labels.get(inventory.LABEL),
    }


def read_bounded_stream(
    process: subprocess.Popen[bytes], *, limit: int, timeout: float, clock: Callable[[], float]
) -> bytes:
    """Read a subprocess's stdout up to ``limit`` bytes within ``timeout``."""
    assert process.stdout is not None
    deadline = clock() + timeout
    chunks: list[bytes] = []
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise ExportError(f"archive stream exceeded {timeout}s")
            if not selector.select(remaining):
                continue
            chunk = os.read(process.stdout.fileno(), _READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ExportError(f"archive stream exceeds {limit} bytes")
            chunks.append(chunk)
    try:
        process.wait(timeout=max(deadline - clock(), 0.1))
    except subprocess.TimeoutExpired as error:
        raise ExportError("archive copy did not exit") from error
    return b"".join(chunks)


def copy_archive(container: str, path: str) -> bytes:
    """Stream ``docker cp CONTAINER:PATH -`` into memory, bounded in size and time."""
    process = subprocess.Popen(
        ["docker", "cp", f"{container}:{path}", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        data = read_bounded_stream(
            process, limit=MAX_ARCHIVE_BYTES, timeout=COPY_TIMEOUT_SECONDS, clock=time.monotonic
        )
    finally:
        if process.poll() is None:
            process.kill()
        stderr = process.communicate(timeout=DOCKER_TIMEOUT_SECONDS)[1]
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise ExportError(f"docker cp exited {process.returncode}: {detail}")
    return data


def inventory_from_archive(archive: bytes, basename: str) -> bytes:
    """Return the sole regular-file member's bytes; reject anything else."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            members = bundle.getmembers()
            if len(members) != 1:
                raise ExportError(f"archive has {len(members)} members; expected one file")
            member = members[0]
            if member.name != basename:
                raise ExportError(f"unexpected archive member name {member.name!r}")
            if not member.isreg():
                raise ExportError(f"archive member {member.name!r} is not a regular file")
            if member.size > inventory.MAX_INVENTORY_BYTES:
                raise ExportError(f"inventory member is {member.size} bytes")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ExportError("inventory member has no readable payload")
            return stream.read(inventory.MAX_INVENTORY_BYTES + 1)
    except tarfile.TarError as error:
        raise ExportError(f"inventory archive is malformed: {error}") from error


def copy_inventory(image_id: str) -> bytes:
    """Copy the fixed inventory path out of a never-started container."""
    container = _docker(
        ["create", "--network", "none", "--entrypoint", "/bin/true", image_id]
    ).strip()
    try:
        archive = copy_archive(container, inventory.INVENTORY_PATH)
        data = inventory_from_archive(archive, PurePosixPath(inventory.INVENTORY_PATH).name)
    except BaseException as error:
        # Clean up without masking the copy failure that is already in flight.
        try:
            _docker(["rm", "--force", container])
        except (ExportError, OSError) as cleanup_error:
            detail = f"container {container} cleanup failed: {cleanup_error}"
            if isinstance(error, Exception):
                raise ExportError(f"{error}; {detail}") from error
            error.add_note(detail)
        raise
    _docker(["rm", "--force", container])
    return data


def verify_current_state(image_id: str) -> None:
    """Run the installed helper's current-state check in the exact image, offline."""
    _docker(
        [
            "run", "--rm", "--network", "none", "--entrypoint", "python", image_id,
            "-I", inventory.HELPER_PATH, "verify", inventory.INVENTORY_PATH,
        ],
        timeout=VERIFY_TIMEOUT_SECONDS,
    )  # fmt: skip


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_atomically(path, data)


def _summarize(evidence: dict[str, Any], data: bytes) -> None:
    evidence.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    document = inventory.parse_inventory(data)
    evidence.update(
        schema=document["schema"],
        package_count=len(document["packages"]),
        python_version=document["python"]["version"],
        pip_version=document["python"]["pip_version"],
    )


def _compare(evidence: dict[str, Any], data: bytes, expected: Path) -> None:
    expected_bytes = inventory.read_inventory_file(expected)
    matches = expected_bytes == data
    evidence["expected_inventory"] = {
        "path": str(expected),
        "sha256": hashlib.sha256(expected_bytes).hexdigest(),
        "matches": matches,
    }
    if not matches:
        raise ExportError(f"inventory bytes differ from expected parent inventory {expected}")


def export(args: argparse.Namespace, evidence: dict[str, Any]) -> None:
    """Run every gate, filling ``evidence`` as each fact becomes known."""
    identity = inspect_image(args.image)
    evidence.update(identity)
    if identity["label"] != inventory.INVENTORY_PATH:
        raise ExportError(f"label {inventory.LABEL} is {identity['label']!r}")
    data = copy_inventory(identity["image_id"])
    try:
        _summarize(evidence, data)
    except inventory.InventoryError:
        rejected = args.output.with_name(args.output.name + ".rejected")
        _write(rejected, data)
        evidence["rejected_output"] = str(rejected)
        raise
    _write(args.output, data)
    evidence["output"] = str(args.output)
    if args.expected_inventory is not None:
        _compare(evidence, data, args.expected_inventory)
    if args.verify_current_state:
        verify_current_state(identity["image_id"])
        evidence["current_state_verified"] = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="exact local image reference")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--expected-inventory", type=Path, help="parent inventory to match")
    parser.add_argument("--base-reference", help="selected runtime base, recorded only")
    parser.add_argument("--verify-current-state", action="store_true")
    args = parser.parse_args(argv)
    evidence: dict[str, Any] = {
        "schema": 1,
        "requested_reference": args.image,
        "base_reference": args.base_reference,
        "path": inventory.INVENTORY_PATH,
        "label": None,
        "errors": [],
    }
    try:
        export(args, evidence)
    except (ExportError, inventory.InventoryError, OSError) as error:
        messages = getattr(error, "errors", None) or (str(error),)
        evidence["errors"].extend(messages)
    evidence["status"] = "failed" if evidence["errors"] else "passed"
    _write(args.evidence, (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode())
    for message in evidence["errors"]:
        print(f"ERROR: {message}", file=sys.stderr)
    if not evidence["errors"]:
        print(f"Exported {evidence['package_count']} packages from {evidence['image_id']}")
    return 1 if evidence["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
