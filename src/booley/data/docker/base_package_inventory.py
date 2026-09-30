#!/usr/bin/env python3
"""Record, validate, and verify the runtime base's resolved package inventory.

The stable runtime base carries no Booley distribution, so this helper is
standard-library-only. Dockerfile.base installs it at a fixed root-owned path
and always runs it as ``python -I`` so user-site packages and ``PYTHON*``
environment overrides cannot change what is collected.

The inventory is informational provenance for the final runtime base only: the
installed dpkg packages plus the exact interpreter and pip versions. It is not
an SBOM, a lockfile, or an image identity; derived images inherit its bytes
unchanged even when their own layers add packages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

SCHEMA = 1
SCOPE = "runtime-base"
INVENTORY_PATH = "/usr/local/share/booley/base-package-inventory.json"
HELPER_PATH = "/usr/local/libexec/booley/base_package_inventory.py"
LABEL = "io.booley.runtime-base.package-inventory"
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 60

# One tab-separated row per package. db:Status-Status and db:Status-Eflag are
# queried independently of the desired action so held packages stay visible.
DPKG_FORMAT = (
    "${Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\t${db:Status-Eflag}\\n"
)
_DPKG_FIELD_COUNT = 5
_INCLUDED_STATUS = "installed"
_ABSENT_STATUSES = frozenset({"not-installed", "config-files"})
_FINAL_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_TOP_LEVEL_KEYS = frozenset({"schema", "scope", "python", "packages"})
_PYTHON_KEYS = frozenset({"version", "pip_version"})
_PACKAGE_KEYS = frozenset({"name", "version", "architecture"})


class InventoryError(Exception):
    """The inventory could not be collected, or its content is invalid."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class Package(NamedTuple):
    """One installed dpkg package; identity is the (name, architecture) pair."""

    name: str
    version: str
    architecture: str


CommandRunner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def parse_dpkg_rows(output: str) -> list[Package]:
    """Parse ``dpkg-query -W`` rows in :data:`DPKG_FORMAT` order.

    Installed packages are kept regardless of hold/desired action. Fully
    absent and config-only packages are dropped; every intermediate state or
    non-``ok`` error flag is rejected rather than silently omitted.
    """
    packages: list[Package] = []
    errors: list[str] = []
    for number, line in enumerate(output.splitlines(), start=1):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != _DPKG_FIELD_COUNT:
            errors.append(
                f"dpkg row {number}: expected {_DPKG_FIELD_COUNT} fields, got {len(fields)}"
            )
            continue
        name, version, architecture, status, error_flag = fields
        if error_flag != "ok":
            errors.append(f"dpkg row {number}: {name} has error flag {error_flag!r}")
        elif status in _ABSENT_STATUSES:
            continue
        elif status != _INCLUDED_STATUS:
            errors.append(f"dpkg row {number}: {name} is in unhealthy state {status!r}")
        elif not (name and version and architecture):
            errors.append(f"dpkg row {number}: installed package has an empty field")
        else:
            packages.append(Package(name, version, architecture))
    if errors:
        raise InventoryError(errors)
    return packages


def build_inventory(
    packages: Iterable[Package], python_version: str, pip_version: str
) -> dict[str, Any]:
    """Return the canonical, validated v1 inventory document."""
    rows = sorted(packages, key=lambda package: (package.name, package.architecture))
    document = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "python": {"version": python_version, "pip_version": pip_version},
        "packages": [package._asdict() for package in rows],
    }
    errors = validate_inventory(document)
    if errors:
        raise InventoryError(errors)
    return document


def serialize(document: Mapping[str, Any]) -> bytes:
    """Serialize deterministically: UTF-8, sorted keys, one trailing newline."""
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    if len(data) > MAX_INVENTORY_BYTES:
        raise InventoryError([f"inventory is {len(data)} bytes; limit is {MAX_INVENTORY_BYTES}"])
    return data


def _nonempty_string(value: object, path: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{path}: expected a nonempty string")
        return None
    return value


def _exact_keys(value: object, keys: frozenset[str], path: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: expected an object")
        return False
    missing = sorted(keys - value.keys())
    unknown = sorted(value.keys() - keys)
    if missing:
        errors.append(f"{path}: missing fields {missing}")
    if unknown:
        errors.append(f"{path}: unknown fields {unknown}")
    return not missing


def _python_errors(value: object, errors: list[str]) -> None:
    if not _exact_keys(value, _PYTHON_KEYS, "python", errors):
        return
    assert isinstance(value, dict)
    version = _nonempty_string(value["version"], "python.version", errors)
    if version is not None and not _FINAL_PYTHON_VERSION.fullmatch(version):
        errors.append(f"python.version: {version!r} is not a final major.minor.patch release")
    _nonempty_string(value["pip_version"], "python.pip_version", errors)


def _package_errors(value: object, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append("packages: expected a nonempty list")
        return
    previous: tuple[str, str] | None = None
    for index, row in enumerate(value):
        path = f"packages[{index}]"
        if not _exact_keys(row, _PACKAGE_KEYS, path, errors):
            previous = None
            continue
        assert isinstance(row, dict)
        name = _nonempty_string(row["name"], f"{path}.name", errors)
        _nonempty_string(row["version"], f"{path}.version", errors)
        architecture = _nonempty_string(row["architecture"], f"{path}.architecture", errors)
        if name is None or architecture is None:
            previous = None
            continue
        identity = (name, architecture)
        if previous == identity:
            errors.append(f"{path}: duplicate package identity {name}:{architecture}")
        elif previous is not None and identity < previous:
            errors.append(f"{path}: {name}:{architecture} is not sorted by (name, architecture)")
        previous = identity


def validate_inventory(document: object) -> list[str]:
    """Return every v1 contract violation in ``document`` (empty when valid)."""
    errors: list[str] = []
    if not _exact_keys(document, _TOP_LEVEL_KEYS, "inventory", errors):
        return errors
    assert isinstance(document, dict)
    schema = document["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        errors.append(f"schema: expected integer {SCHEMA}, got {schema!r}")
    if document["scope"] != SCOPE:
        errors.append(f"scope: expected {SCOPE!r}, got {document['scope']!r}")
    _python_errors(document["python"], errors)
    _package_errors(document["packages"], errors)
    return errors


def parse_inventory(data: bytes) -> dict[str, Any]:
    """Decode and validate inventory bytes, raising with every violation."""
    if len(data) > MAX_INVENTORY_BYTES:
        raise InventoryError([f"inventory is {len(data)} bytes; limit is {MAX_INVENTORY_BYTES}"])
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InventoryError([f"inventory is not UTF-8 JSON: {error}"]) from error
    errors = validate_inventory(document)
    if errors:
        raise InventoryError(errors)
    return document


def read_inventory_file(path: Path) -> bytes:
    """Read at most the size limit plus one byte, rejecting oversized files."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_INVENTORY_BYTES + 1)
    except OSError as error:
        raise InventoryError([f"cannot read {path}: {error}"]) from error
    if len(data) > MAX_INVENTORY_BYTES:
        raise InventoryError([f"{path} exceeds {MAX_INVENTORY_BYTES} bytes"])
    return data


def collect_dpkg(run: CommandRunner = subprocess.run) -> list[Package]:
    """Query dpkg under the C locale with a bounded time and output size."""
    environment = {"LC_ALL": "C", "LANG": "C", "PATH": os.environ.get("PATH", os.defpath)}
    try:
        result = run(
            ["dpkg-query", "-W", "-f", DPKG_FORMAT],
            capture_output=True,
            check=False,
            env=environment,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InventoryError([f"dpkg-query failed: {error}"]) from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise InventoryError([f"dpkg-query exited {result.returncode}: {detail}"])
    if len(result.stdout) > MAX_INVENTORY_BYTES:
        raise InventoryError([f"dpkg-query output exceeds {MAX_INVENTORY_BYTES} bytes"])
    return parse_dpkg_rows(result.stdout.decode("utf-8"))


def python_versions() -> tuple[str, str]:
    """Return this interpreter's version and its agreeing pip version."""
    import importlib.metadata
    import platform

    try:
        import pip
    except ImportError as error:
        raise InventoryError([f"pip is not importable: {error}"]) from error
    try:
        metadata_version = importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError as error:
        raise InventoryError(["pip distribution metadata is absent"]) from error
    imported_version = getattr(pip, "__version__", None)
    if imported_version != metadata_version:
        raise InventoryError(
            [f"imported pip {imported_version!r} disagrees with metadata {metadata_version!r}"]
        )
    return platform.python_version(), metadata_version


def collect_inventory(run: CommandRunner = subprocess.run) -> dict[str, Any]:
    """Collect the current base package state as a validated document."""
    python_version, pip_version = python_versions()
    return build_inventory(collect_dpkg(run), python_version, pip_version)


def write_atomically(path: Path, data: bytes) -> None:
    """Publish ``data`` at ``path`` with mode 0644, never as a partial file."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_current(stored: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Compare stored and freshly collected content semantically."""
    errors: list[str] = []
    if stored["python"] != current["python"]:
        errors.append(f"python: stored {stored['python']} but current {current['python']}")
    stored_rows = {
        (row["name"], row["architecture"]): row["version"] for row in stored["packages"]
    }
    current_rows = {
        (row["name"], row["architecture"]): row["version"] for row in current["packages"]
    }
    for identity in sorted(stored_rows.keys() | current_rows.keys()):
        before, after = stored_rows.get(identity), current_rows.get(identity)
        if before != after:
            label = ":".join(identity)
            errors.append(
                f"package {label}: stored {before or '<absent>'}, current {after or '<absent>'}"
            )
    return errors


def _require_isolated() -> None:
    if not sys.flags.isolated:
        raise InventoryError(["collection must run as `python -I` to ignore user overrides"])


def _generate(output: Path) -> None:
    _require_isolated()
    write_atomically(output, serialize(collect_inventory()))


def _validate(path: Path) -> dict[str, Any]:
    return parse_inventory(read_inventory_file(path))


def _verify(path: Path) -> None:
    _require_isolated()
    errors = verify_current(_validate(path), collect_inventory())
    if errors:
        raise InventoryError(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="collect and write the inventory")
    generate.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="check a file against the v1 schema")
    validate.add_argument("path", type=Path)
    verify = commands.add_parser("verify", help="compare a file with the current base state")
    verify.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            _generate(args.output)
        elif args.command == "validate":
            _validate(args.path)
        else:
            _verify(args.path)
    except InventoryError as error:
        for message in error.errors:
            print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"base package inventory {args.command}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
