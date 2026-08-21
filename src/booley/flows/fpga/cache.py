"""Content-addressed cache for completed FPGA implementation artifacts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.eda.vivado import POLICY_REVISION, SUPPORTED_VERSION

CACHE_SCHEMA = 2
CACHE_FILE = ".booley-fpga-cache.json"
_IMPLEMENTATION_REVISION = 1
_REPORT_PATTERNS = (
    "*_utilization_placed.rpt",
    "*_timing_summary_routed.rpt",
    "*_drc_routed.rpt",
)


@dataclass(frozen=True)
class CacheHit:
    """Validated artifacts belonging to one exact input fingerprint."""

    fingerprint: str
    report_text: str
    producer_evidence: dict[str, Any]


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def input_fingerprint(
    resolved: Any,
    edam: Mapping[str, Any],
    *,
    out_of_context: bool,
) -> str:
    """Hash the resolved design intent, every input byte, and tool identity."""
    files: list[dict[str, Any]] = []
    for item in resolved.rtl_files:
        path = item.absolute(resolved.build_root)
        try:
            digest, size = _hash_file(path)
            missing = False
        except OSError:
            digest, size, missing = hashlib.sha256(b"").hexdigest(), 0, True
        files.append(
            {
                "name": item.name,
                "file_type": item.file_type,
                "tags": list(item.tags),
                "core": item.core,
                "is_include": item.is_include,
                "sha256": digest,
                "size": size,
                "missing": missing,
            }
        )
    payload = {
        "schema": CACHE_SCHEMA,
        "implementation_revision": _IMPLEMENTATION_REVISION,
        "target": {
            "name": resolved.name,
            "vlnv": resolved.vlnv,
            "toplevel": resolved.toplevel,
            "eda_tool": resolved.eda_tool,
            "flow_options": dict(resolved.flow_options),
            "parameters": dict(resolved.parameters),
            "out_of_context": out_of_context,
        },
        "edam": dict(edam),
        "files": files,
        "tool": {
            "name": "vivado",
            "version": SUPPORTED_VERSION,
            "policy_revision": POLICY_REVISION,
            "edalize": _package_version("edalize"),
            "fusesoc": _package_version("fusesoc"),
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fresh(path: Path, min_mtime: float | None) -> bool:
    if min_mtime is None:
        return True
    try:
        return path.stat().st_mtime >= min_mtime
    except OSError:
        return False


def _artifact_paths(
    work_root: Path,
    *,
    require_bitstream: bool,
    min_mtime: float | None = None,
) -> list[Path] | None:
    artifacts: list[Path] = []
    impl_dirs = sorted(path for path in work_root.glob("*.runs/impl_1") if path.is_dir())
    if not impl_dirs:
        return None
    for pattern in _REPORT_PATTERNS:
        matches = sorted(
            path for impl in impl_dirs for path in impl.glob(pattern) if _fresh(path, min_mtime)
        )
        if not matches:
            return None
        artifacts.extend(matches)
    runlogs = [
        impl / "runme.log"
        for impl in impl_dirs
        if (impl / "runme.log").is_file() and _fresh(impl / "runme.log", min_mtime)
    ]
    if not runlogs:
        return None
    artifacts.extend(runlogs)
    bitstreams = sorted(
        path for impl in impl_dirs for path in impl.glob("*.bit") if _fresh(path, min_mtime)
    )
    if require_bitstream and not bitstreams:
        return None
    artifacts.extend(bitstreams)
    return sorted(set(artifacts))


def store(
    work_root: Path,
    fingerprint: str,
    *,
    require_bitstream: bool,
    producer_evidence: dict[str, Any],
    min_mtime: float | None = None,
) -> bool:
    """Atomically record validated artifact digests after a successful route."""
    if not _valid_producer_evidence(producer_evidence):
        return False
    paths = _artifact_paths(
        work_root,
        require_bitstream=require_bitstream,
        min_mtime=min_mtime,
    )
    if paths is None:
        return False
    artifacts: list[dict[str, Any]] = []
    try:
        for path in paths:
            digest, size = _hash_file(path)
            artifacts.append(
                {
                    "path": path.relative_to(work_root).as_posix(),
                    "sha256": digest,
                    "size": size,
                }
            )
    except (OSError, ValueError):
        return False
    payload = {
        "schema": CACHE_SCHEMA,
        "fingerprint": fingerprint,
        "require_bitstream": require_bitstream,
        "producer_evidence": producer_evidence,
        "artifacts": artifacts,
    }
    work_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".fpga-cache.", dir=work_root)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(work_root / CACHE_FILE)
    except OSError:
        temp_path.unlink(missing_ok=True)
        return False
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def load(
    work_root: Path,
    fingerprint: str,
    *,
    require_bitstream: bool,
) -> CacheHit | None:
    """Return a hit only when metadata and every artifact byte still match."""
    raw = _read_metadata(work_root)
    if not _metadata_matches(raw, fingerprint, require_bitstream):
        return None
    assert raw is not None
    expected_paths = _artifact_paths(work_root, require_bitstream=require_bitstream)
    if expected_paths is None or not _artifacts_match(work_root, raw["artifacts"], expected_paths):
        return None
    parts = _read_report_text(expected_paths)
    if parts is None:
        return None
    return CacheHit(
        fingerprint=fingerprint,
        report_text="\n".join(parts),
        producer_evidence=dict(raw["producer_evidence"]),
    )


def _read_metadata(work_root: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((work_root / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _metadata_matches(
    raw: dict[str, Any] | None,
    fingerprint: str,
    require_bitstream: bool,
) -> bool:
    return bool(
        raw is not None
        and raw.get("schema") == CACHE_SCHEMA
        and raw.get("fingerprint") == fingerprint
        and raw.get("require_bitstream") is require_bitstream
        and _valid_producer_evidence(raw.get("producer_evidence"))
        and isinstance(raw.get("artifacts"), list)
    )


def _valid_producer_evidence(value: Any) -> bool:
    """Return whether cache metadata identifies its producing run."""
    if not isinstance(value, dict) or value.get("version") != 1:
        return False
    return all(
        isinstance(value.get(key), str) and bool(value[key])
        for key in ("run_id", "source_revision", "source_sha256", "recipe_sha256")
    )


def _artifacts_match(
    work_root: Path,
    records: list[Any],
    expected_paths: list[Path],
) -> bool:
    expected_rel = {path.relative_to(work_root).as_posix() for path in expected_paths}
    recorded_rel: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return False
        path = (work_root / item["path"]).resolve()
        if not path.is_relative_to(work_root.resolve()) or not path.is_file():
            return False
        try:
            digest, size = _hash_file(path)
        except OSError:
            return False
        if digest != item.get("sha256") or size != item.get("size"):
            return False
        recorded_rel.add(item["path"])
    return recorded_rel == expected_rel


def _read_report_text(paths: list[Path]) -> list[str] | None:
    parts: list[str] = []
    try:
        for path in paths:
            if path.suffix != ".bit":
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return parts
