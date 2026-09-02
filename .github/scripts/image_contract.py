#!/usr/bin/env python3
"""Validate a built Session Runtime image against its declared contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import require_dict, require_list, require_str

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


def _probes(section: dict) -> list[dict[str, object]]:
    probes: list[dict[str, object]] = []
    for raw in require_list(section.get("probe", []), field="probe"):
        probe = require_dict(raw, field="probe entry")
        timeout = probe.get("timeout_seconds", 60)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise ValueError("probe timeout_seconds must be a positive integer")
        probes.append(
            {
                "name": require_str(probe, "name"),
                "command": require_str(probe, "command"),
                "timeout_seconds": timeout,
            }
        )
    return probes


def _normalized_section(section: dict) -> dict[str, object]:
    return {
        field: _string_list(section, field)
        for field in ("required_commands", "required_paths", "absent_paths", "stripped_elf")
    } | {"hard_link_groups": _link_groups(section), "probes": _probes(section)}


def load_contract(path: Path, flavor: str) -> dict[str, object]:
    document = require_dict(tomllib.loads(path.read_text(encoding="utf-8")), field="contract")
    if document.get("schema") != 1:
        raise ValueError("runtime contract schema must be 1")
    if flavor not in {"standard", "riscv"}:
        raise ValueError(f"unsupported image flavor: {flavor}")
    common = _normalized_section(require_dict(document.get("common"), field="common contract"))
    specific = _normalized_section(
        require_dict(document.get(flavor, {}), field=f"{flavor} contract")
    )
    return {
        field: [
            *require_list(common[field], field=field),
            *require_list(specific[field], field=field),
        ]
        for field in common
    }


def _docker_json(argv: list[str]) -> object:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def _image_identity(image: str) -> dict[str, object]:
    rows = require_list(_docker_json(["image", "inspect", image]), field="image inspect")
    if len(rows) != 1:
        raise ValueError(f"Docker returned {len(rows)} inspect rows for {image!r}")
    row = require_dict(rows[0], field="image inspect row")
    rootfs = require_dict(row.get("RootFS"), field="image RootFS")
    config = require_dict(row.get("Config"), field="image Config")
    return {
        "reference": image,
        "image_id": require_str(row, "Id"),
        "os": require_str(row, "Os"),
        "architecture": require_str(row, "Architecture"),
        "runtime_user": require_str(config, "User"),
        "working_dir": require_str(config, "WorkingDir"),
        "rootfs_diff_ids": _string_list(rootfs, "Layers"),
    }


def _probe_image(image: str, contract: dict[str, object]) -> dict:
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


def _layer_contract(image: dict[str, object], base: dict[str, object] | None) -> dict:
    image_layers = require_list(image["rootfs_diff_ids"], field="image diff IDs")
    if base is None:
        return {"base_reference": None, "prefix_match": None, "additional_layer_count": None}
    base_layers = require_list(base["rootfs_diff_ids"], field="base diff IDs")
    prefix_match = image_layers[: len(base_layers)] == base_layers
    additional = len(image_layers) - len(base_layers)
    return {
        "base_reference": base["reference"],
        "base_image_id": base["image_id"],
        "prefix_match": prefix_match,
        "additional_layer_count": additional,
    }


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate(image: str, flavor: str, contract_path: Path, base_image: str | None) -> dict:
    contract = load_contract(contract_path, flavor)
    identity = _image_identity(image)
    base = _image_identity(base_image) if base_image else None
    layer_contract = _layer_contract(identity, base)
    probe = _probe_image(image, contract)
    errors = list(require_list(probe.get("errors"), field="probe errors"))
    if base is not None and not layer_contract["prefix_match"]:
        errors.append("derived image RootFS layers do not prefix-match the standard image")
    if base is not None and layer_contract["additional_layer_count"] < 1:
        errors.append("derived image added no RootFS layer")
    return {
        "schema": 1,
        "measured_at": _timestamp(),
        "contract": str(contract_path),
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "flavor": flavor,
        "image": identity,
        "layers": layer_contract,
        "assertions": probe,
        "errors": errors,
    }


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
    errors = require_list(evidence["errors"], field="validation errors")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Session Runtime contract passed for {args.image} ({args.flavor})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
