#!/usr/bin/env python3
"""Validate a built Sandbox Image against its declared contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import subprocess
import tarfile
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import BinaryIO, NotRequired, TypedDict

from booley.core.boundary import as_str_list, require_dict, require_int, require_list, require_str
from booley.runtime.timefmt import utc_now_rfc3339

try:  # Python >= 3.14 only; older interpreters fail closed on zstd layers.
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

# Upper bound for one `docker save` stream (coding principle 16).
_DOCKER_SAVE_DEADLINE_SECONDS = 900
# Grace for `docker save` to exit after closing its output stream.
_DOCKER_SAVE_EXIT_GRACE_SECONDS = 60
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ProbeDefinition(TypedDict):
    name: str
    command: str
    timeout_seconds: int


class RuntimeContract(TypedDict):
    required_commands: list[str]
    required_paths: list[str]
    absent_paths: list[str]
    stripped_elf: list[str]
    hard_link_groups: list[list[str]]
    probes: list[ProbeDefinition]


class ImageIdentity(TypedDict):
    reference: str
    image_id: str
    os: str
    architecture: str
    runtime_user: str
    working_dir: str
    rootfs_diff_ids: list[str]


class LayerContract(TypedDict):
    base_reference: str | None
    base_image_id: NotRequired[str]
    prefix_match: bool | None
    additional_layer_count: int | None


class ValidationEvidence(TypedDict):
    schema: int
    measured_at: str
    contract: str
    contract_sha256: str
    flavor: str
    image: ImageIdentity
    layers: LayerContract
    assertions: dict
    layer_links: list[dict]
    errors: list[str]


_CONTAINER_PROBE = r"""
import hashlib
import json
import os
import shutil
import subprocess
import sys


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check_commands(contract, report):
    for command in contract["required_commands"]:
        resolved = shutil.which(command)
        report["commands"][command] = resolved
        if resolved is None:
            report["errors"].append(f"required command is absent: {command}")


def check_paths(contract, report):
    for path in contract["required_paths"]:
        exists = os.path.exists(path)
        row = {"path": path, "exists": exists}
        if exists:
            status = os.stat(path)
            row.update({"bytes": status.st_size, "mode": status.st_mode & 0o7777})
        else:
            report["errors"].append(f"required path is absent: {path}")
        report["paths"].append(row)


def check_absent_paths(contract, report):
    for path in contract["absent_paths"]:
        absent = not os.path.lexists(path)
        report["absent_paths"].append({"path": path, "absent": absent})
        if not absent:
            report["errors"].append(f"forbidden payload is present: {path}")


def check_stripped_elf(contract, report):
    for path in contract["stripped_elf"]:
        result = subprocess.run(["file", "-b", path], text=True, capture_output=True)
        description = result.stdout.strip()
        stripped = result.returncode == 0 and "stripped" in description and "not stripped" not in description
        row = {"path": path, "stripped": stripped, "description": description}
        if result.returncode == 0:
            row["sha256"] = digest(path)
        if not stripped:
            report["errors"].append(f"ELF is not stripped: {path}")
        report["stripped_elf"].append(row)


def check_hard_links(contract, report):
    for paths in contract["hard_link_groups"]:
        rows = []
        identities = set()
        hashes = set()
        for path in paths:
            try:
                status = os.stat(path)
            except OSError as exc:
                rows.append({"path": path, "error": str(exc)})
                report["errors"].append(f"hard-link path is unavailable: {path}: {exc}")
                continue
            checksum = digest(path)
            identities.add((status.st_dev, status.st_ino))
            hashes.add(checksum)
            rows.append({"path": path, "device": status.st_dev, "inode": status.st_ino,
                         "link_count": status.st_nlink, "sha256": checksum})
        complete = len(rows) == len(paths) and all("link_count" in row for row in rows)
        linked = complete and len(identities) == 1 and len(hashes) == 1 \
            and rows[0]["link_count"] >= len(paths)
        report["hard_links"].append({"paths": rows, "same_inode": linked})
        if not linked:
            report["errors"].append(f"paths are not one retained hard-link group: {paths}")


def run_probes(contract, report):
    for probe in contract["probes"]:
        try:
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", probe["command"]],
                                    text=True, capture_output=True,
                                    timeout=probe["timeout_seconds"])
            row = {"name": probe["name"], "returncode": result.returncode,
                   "stdout": result.stdout[-8000:], "stderr": result.stderr[-8000:]}
        except subprocess.TimeoutExpired as exc:
            row = {"name": probe["name"], "returncode": None,
                   "stdout": str(exc.stdout or "")[-8000:],
                   "stderr": str(exc.stderr or "")[-8000:]}
        report["probes"].append(row)
        if row["returncode"] != 0:
            report["errors"].append(f"behavior probe failed: {probe['name']}")


def main():
    contract = json.load(sys.stdin)
    report = {"commands": {}, "paths": [], "absent_paths": [], "stripped_elf": [],
              "hard_links": [], "probes": [], "errors": []}
    check_commands(contract, report)
    check_paths(contract, report)
    check_absent_paths(contract, report)
    check_stripped_elf(contract, report)
    check_hard_links(contract, report)
    run_probes(contract, report)
    json.dump(report, sys.stdout, sort_keys=True)


main()
"""


def _string_list(section: dict, field: str) -> list[str]:
    return [
        require_str({"value": value}, "value")
        for value in require_list(section.get(field, []), field=field)
    ]


def _link_groups(section: dict) -> list[list[str]]:
    groups: list[list[str]] = []
    for raw in require_list(section.get("hard_link_groups", []), field="hard_link_groups"):
        group = [
            require_str({"value": value}, "value")
            for value in require_list(raw, field="hard-link group")
        ]
        if len(group) < 2:
            raise ValueError("hard-link groups must contain at least two paths")
        groups.append(group)
    return groups


def _probes(section: dict) -> list[ProbeDefinition]:
    probes: list[ProbeDefinition] = []
    for raw in require_list(section.get("probe", []), field="probe"):
        probe = require_dict(raw, field="probe entry")
        timeout = require_int(probe.get("timeout_seconds", 60), field="probe timeout_seconds")
        if timeout < 1:
            raise ValueError("probe timeout_seconds must be a positive integer")
        probes.append(
            {
                "name": require_str(probe, "name"),
                "command": require_str(probe, "command"),
                "timeout_seconds": timeout,
            }
        )
    return probes


def _normalized_section(section: dict) -> RuntimeContract:
    return RuntimeContract(
        required_commands=_string_list(section, "required_commands"),
        required_paths=_string_list(section, "required_paths"),
        absent_paths=_string_list(section, "absent_paths"),
        stripped_elf=_string_list(section, "stripped_elf"),
        hard_link_groups=_link_groups(section),
        probes=_probes(section),
    )


def load_contract(path: Path, flavor: str) -> RuntimeContract:
    document = require_dict(tomllib.loads(path.read_text(encoding="utf-8")), field="contract")
    if require_int(document.get("schema"), field="runtime contract schema") != 1:
        raise ValueError("runtime contract schema must be 1")
    if flavor not in {"standard", "riscv"}:
        raise ValueError(f"unsupported image flavor: {flavor}")
    common = _normalized_section(require_dict(document.get("common"), field="common contract"))
    specific = _normalized_section(
        require_dict(document.get(flavor, {}), field=f"{flavor} contract")
    )
    return RuntimeContract(
        required_commands=[*common["required_commands"], *specific["required_commands"]],
        required_paths=[*common["required_paths"], *specific["required_paths"]],
        absent_paths=[*common["absent_paths"], *specific["absent_paths"]],
        stripped_elf=[*common["stripped_elf"], *specific["stripped_elf"]],
        hard_link_groups=[*common["hard_link_groups"], *specific["hard_link_groups"]],
        probes=[*common["probes"], *specific["probes"]],
    )


def _docker_json(argv: list[str]) -> object:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def _image_identity(image: str) -> ImageIdentity:
    rows = require_list(_docker_json(["image", "inspect", image]), field="image inspect")
    if len(rows) != 1:
        raise ValueError(f"Docker returned {len(rows)} inspect rows for {image!r}")
    row = require_dict(rows[0], field="image inspect row")
    rootfs = require_dict(row.get("RootFS"), field="image RootFS")
    config = require_dict(row.get("Config"), field="image Config")
    return ImageIdentity(
        reference=image,
        image_id=require_str(row, "Id"),
        os=require_str(row, "Os"),
        architecture=require_str(row, "Architecture"),
        runtime_user=require_str(config, "User"),
        working_dir=require_str(config, "WorkingDir"),
        rootfs_diff_ids=_string_list(rootfs, "Layers"),
    )


def _probe_image(image: str, contract: RuntimeContract) -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "-i",
            "--rm",
            "--init",
            "--network",
            "none",
            "--tmpfs",
            "/home/agent:uid=1000,gid=1000,mode=700",
            "--env",
            "HOME=/home/agent",
            "--entrypoint",
            "python3",
            image,
            "-c",
            _CONTAINER_PROBE,
        ],
        input=json.dumps(contract),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return require_dict(json.loads(result.stdout), field="container probe report")


def _normalized(name: str) -> str:
    """Tar member name without a leading ``./`` or ``/``."""
    return posixpath.normpath(name).lstrip("/") if name not in ("", ".") else ""


def _open_layer(stream: BinaryIO) -> tarfile.TarFile:
    """Open one layer blob as a streaming tar, sniffing zstd from its magic."""
    if not hasattr(stream, "peek"):
        raise tarfile.ReadError("layer stream cannot be sniffed")
    if stream.peek(4)[:4] == _ZSTD_MAGIC:
        if _zstd is None:
            raise _ZstdUnavailableError
        return tarfile.open(fileobj=_zstd.ZstdFile(stream), mode="r|")
    return tarfile.open(fileobj=stream, mode="r|*")


def _scan_layer(stream: BinaryIO, wanted: set[str]) -> list[dict]:
    """Group-path entries of one layer tar (plain, gzip, bzip2, xz or zstd)."""
    found: list[dict] = []
    with _open_layer(stream) as inner:
        for member in inner:
            name = _normalized(member.name)
            if name not in wanted:
                continue
            kind = (
                "file"
                if member.isreg()
                else "hardlink"
                if member.islnk()
                else "symlink"
                if member.issym()
                else "other"
            )
            found.append(
                {
                    "path": "/" + name,
                    "type": kind,
                    "linkname": _normalized(member.linkname) if member.linkname else "",
                    "size": member.size,
                }
            )
    return found


class _ZstdUnavailableError(Exception):
    """A zstd layer was met on an interpreter without ``compression.zstd``."""


def _read_export(
    archive: BinaryIO, wanted: set[str]
) -> tuple[dict[str, list[dict] | str], dict[str, str], object]:
    """Stream a `docker save` archive: per-member layer scans, links, manifest."""
    scans: dict[str, list[dict] | str] = {}
    links: dict[str, str] = {}
    manifest: object = None
    with tarfile.open(fileobj=archive, mode="r|*") as outer:
        for member in outer:
            if member.issym():
                links[member.name] = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
                continue
            if not member.isreg():
                continue
            stream = outer.extractfile(member)
            if stream is None:
                continue
            if member.name == "manifest.json":
                manifest = json.loads(stream.read())
                continue
            try:
                scans[member.name] = _scan_layer(stream, wanted)
            except _ZstdUnavailableError:
                scans[member.name] = "zstd"
            except (tarfile.TarError, OSError, EOFError):
                scans[member.name] = "unreadable"  # e.g. a JSON blob; only fatal if a layer
    return scans, links, manifest


def _group_errors(paths: list[str], carriers: dict[int, list[dict]]) -> list[str]:
    """Rules (a) and (b) for one hard-link group."""
    if len(carriers) != 1:
        return [
            f"hard-link group is carried by {len(carriers)} exported layers (expected 1): {paths}"
        ]
    ((index, rows),) = carriers.items()
    files = [row for row in rows if row["type"] == "file"]
    linked = all(
        row["type"] == "hardlink" and files and row["linkname"] == _normalized(files[0]["path"])
        for row in rows
        if row["type"] != "file"
    )
    if len(rows) != len(paths) or len(files) != 1 or not linked:
        return [f"hard-link group is not one tar hard-link set in exported layer {index}: {paths}"]
    return []


def audit_layer_links(archive: BinaryIO, groups: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Require each hard-link group to live in one exported layer.

    A layer diff cannot record a hard link whose other name lives only in a
    lower layer, so a group split across layers stores the bytes twice.  This
    inspects the layer tars of a `docker save` stream, which is independent of
    the image store that produced them.
    """
    wanted = {_normalized(path) for paths in groups for path in paths}
    scans, links, manifest = _read_export(archive, wanted)
    entries = manifest if isinstance(manifest, list) else []
    if len(entries) != 1 or not isinstance(entries[0], dict):
        return [], [
            f"exported archive must describe exactly one image (manifest.json has {len(entries)})"
        ]
    layers = as_str_list(entries[0].get("Layers"))
    errors: list[str] = []
    per_layer: list[list[dict]] = []
    for name in layers:
        scan = scans.get(links.get(name, name), "unreadable")
        if scan == "zstd":
            errors.append(
                f"exported layer {name} is zstd-compressed; auditing it needs Python >= 3.14"
            )
        elif isinstance(scan, str):
            errors.append(f"exported layer is unreadable: {name}")
        per_layer.append([] if isinstance(scan, str) else scan)
    if errors:
        return [], errors  # an unreadable layer makes any group verdict unreliable
    rows = [{**row, "layer": layers[i]} for i, found in enumerate(per_layer) for row in found]
    for paths in groups:
        group = {_normalized(path) for path in paths}
        carriers = {
            i: [row for row in found if _normalized(row["path"]) in group]
            for i, found in enumerate(per_layer)
        }
        errors.extend(_group_errors(paths, {i: r for i, r in carriers.items() if r}))
    return rows, errors


def _read_text(stream: BinaryIO) -> str:
    stream.seek(0)
    return stream.read().decode(errors="replace").strip()


def _layer_link_audit(image: str, groups: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Run `docker save` under a deadline and audit its layers."""
    if not groups:
        return [], []
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=stderr)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(_DOCKER_SAVE_DEADLINE_SECONDS, expire)
        timer.daemon = True  # a stray timer must never hold the interpreter open
        timer.start()
        killed_by_us = False
        try:
            try:
                assert proc.stdout is not None
                result = audit_layer_links(proc.stdout, groups)
                # Let docker finish writing so it exits cleanly.  Bounded by the
                # deadline timer above, which kills the process and ends the read.
                while proc.stdout.read(1 << 20):
                    pass
                proc.wait(timeout=_DOCKER_SAVE_EXIT_GRACE_SECONDS)
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
                try:
                    proc.wait(timeout=2)  # an early parser failure may race docker's exit
                except subprocess.TimeoutExpired:
                    killed_by_us = True
                    proc.kill()
                    proc.wait(timeout=60)
        except Exception as exc:
            if expired.is_set():
                raise RuntimeError(
                    f"docker save {image} exceeded its {_DOCKER_SAVE_DEADLINE_SECONDS}s deadline"
                ) from exc
            if killed_by_us and isinstance(exc, subprocess.TimeoutExpired):
                raise RuntimeError(
                    f"docker save {image} closed its output but did not exit; killed"
                ) from exc
            if not killed_by_us and proc.returncode not in (0, None):
                raise RuntimeError(f"docker save {image} failed: {_read_text(stderr)}") from exc
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"docker save {image} failed: {_read_text(stderr)}")
    return result


def _layer_contract(image: ImageIdentity, base: ImageIdentity | None) -> LayerContract:
    image_layers = image["rootfs_diff_ids"]
    if base is None:
        return LayerContract(
            base_reference=None,
            prefix_match=None,
            additional_layer_count=None,
        )
    base_layers = base["rootfs_diff_ids"]
    prefix_match = image_layers[: len(base_layers)] == base_layers
    additional = len(image_layers) - len(base_layers)
    return LayerContract(
        base_reference=base["reference"],
        base_image_id=base["image_id"],
        prefix_match=prefix_match,
        additional_layer_count=additional,
    )


def validate(
    image: str, flavor: str, contract_path: Path, base_image: str | None
) -> ValidationEvidence:
    contract = load_contract(contract_path, flavor)
    identity = _image_identity(image)
    base = _image_identity(base_image) if base_image else None
    layer_contract = _layer_contract(identity, base)
    probe = _probe_image(image, contract)
    errors = _string_list(probe, "errors")
    layer_links, layer_link_errors = _layer_link_audit(image, contract["hard_link_groups"])
    errors.extend(layer_link_errors)
    if base is not None and not layer_contract["prefix_match"]:
        errors.append("derived image RootFS layers do not prefix-match the standard image")
    additional_layer_count = layer_contract["additional_layer_count"]
    if base is not None and (additional_layer_count is None or additional_layer_count < 1):
        errors.append("derived image added no RootFS layer")
    return ValidationEvidence(
        schema=1,
        measured_at=utc_now_rfc3339(),
        contract=str(contract_path),
        contract_sha256=hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        flavor=flavor,
        image=identity,
        layers=layer_contract,
        assertions=probe,
        layer_links=layer_links,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--flavor", required=True, choices=("standard", "riscv"))
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--base-image")
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    evidence = validate(args.image, args.flavor, args.contract, args.base_image)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    if evidence["errors"]:
        for error in evidence["errors"]:
            print(f"ERROR: {error}")
        return 1
    print(f"Sandbox contract passed for {args.image} ({args.flavor})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
