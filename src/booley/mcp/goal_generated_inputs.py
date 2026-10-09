"""Authenticate generated build inputs from the exact selected Simulation producer."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from booley.criteria.evidence_ledger import validated_evidence_records
from booley.criteria.state import DevelopmentState
from booley.flows.sim.campaign.codec import MANIFEST_MAX_BYTES, decode_simulation_campaign_manifest
from booley.fusesoc.fusesoc_registry import core_files_root, read_core
from booley.goals.derivation import selected_observations
from booley.goals.input_view import GeneratedBuildInput
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.goals.recorder import GOAL_SCOPE
from booley.goals.review_package import original_observations
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.regular_file import open_regular_nofollow
from booley.targets.catalog import TargetCatalog
from booley.targets.declared_inputs import core_program_paths, project_config_program_paths


def generated_inputs(
    project: Path, record: GoalRecord, state: DevelopmentState, root: Path
) -> tuple[GeneratedBuildInput, ...]:
    """Only selected observations or their hash-verified originals can authorize data."""
    paths = record_paths(project, record.id)
    records = validated_evidence_records(GOAL_SCOPE, paths.logs_dir, state, {})
    indexed = {row["sequence"]: row for row in records}
    selected = selected_observations(record, state, project)
    origins: dict[int, dict[str, Any]] = {}
    for row in selected.values():
        for original in [row, *original_observations(row, indexed)]:
            origins[original["sequence"]] = original
    result: list[GeneratedBuildInput] = []
    for row in origins.values():
        transaction = row.get("transaction_id")
        if transaction is None:
            continue
        manifest = json.loads(
            (paths.logs_dir / "acceptance/transactions" / f"{transaction}.json").read_bytes()
        )
        envelope = manifest["envelope"]
        producer = _producer_manifest(paths.runtime_dir, envelope)
        if producer is not None:
            result.extend(_disclosed_inputs(root, producer, row, envelope))
    return tuple(result)


def _producer_manifest(runtime: Path, envelope: dict[str, Any]) -> Mapping[str, object] | None:
    runtime = runtime.resolve()
    matches: list[Mapping[str, object]] = []
    for path in sorted((runtime / "flow-reports").rglob("manifest.json")):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            continue
        with os.fdopen(open_regular_nofollow(path), "rb") as source:
            raw = source.read(MANIFEST_MAX_BYTES + 1)
        if (
            len(raw) > MANIFEST_MAX_BYTES
            or "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
            != envelope["manifest_sha256"]
        ):
            continue
        producer = decode_simulation_campaign_manifest(raw).document
        if (
            producer["campaign_id"] != envelope["campaign_id"]
            or producer["origin"] != envelope["origin"]
        ):
            raise LifecycleError("selected campaign manifest has a foreign producer identity")
        matches.append(
            {**producer, "producer_manifest_path": str(path), "producer_manifest_bytes": raw.hex()}
        )
    if len(matches) > 1:
        raise LifecycleError("selected producer has ambiguous duplicate campaign manifests")
    return matches[0] if matches else None


def _disclosed_inputs(
    root: Path, producer: Mapping[str, object], row: dict[str, Any], envelope: dict[str, Any]
) -> list[GeneratedBuildInput]:
    result: list[GeneratedBuildInput] = []
    target = row["detail"]["_source_fingerprint"].get("target")
    if not isinstance(target, str):
        return result
    catalog = TargetCatalog.build(root)
    handle = catalog.select(target)
    identity = handle.identity
    claimed = cast("Mapping[str, object]", producer["target"])
    if identity != f"{claimed['vlnv']}#{claimed['name']}":
        raise LifecycleError("selected generated producer belongs to another Target")
    committed_only = _committed_only_inputs(root, catalog, target)
    for disclosure in cast("tuple[Mapping[str, Any], ...]", producer["planning_disclosures"]):
        for entry in disclosure["generated_files"]:
            if entry["kind"] != "generated_input":
                continue
            relative = Path(entry["path"])
            path = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not path.resolve().is_relative_to(root)
            ):
                raise LifecycleError("generated producer path escapes the selected worktree")
            if path.resolve() in committed_only:
                continue
            result.append(
                GeneratedBuildInput(
                    path,
                    entry["bytes"],
                    entry["sha256"],
                    {
                        "transaction_id": row["transaction_id"],
                        "producer_sequence": row["sequence"],
                        "campaign_id": envelope["campaign_id"],
                        "manifest_sha256": envelope["manifest_sha256"],
                        "planner": disclosure["planner"],
                        "tool_provenance": dict(disclosure["tool_provenance"]),
                        "generated_entry": dict(entry),
                        "producer_manifest_path": producer["producer_manifest_path"],
                        "producer_manifest_bytes": producer["producer_manifest_bytes"],
                    },
                )
            )
    return result


def _committed_only_inputs(root: Path, catalog: TargetCatalog, target: str) -> frozenset[Path]:
    """Runtime HDL types and declared planner programs never acquire a generated exemption."""
    handle = catalog.select(target)
    paths = {
        (root / item.path).resolve()
        for item in catalog.inspect(handle).inputs
        if item.file_type.startswith(("verilogSource", "systemVerilogSource", "vhdlSource"))
    }
    for core in catalog.core_closure((handle,)) or ():
        paths.update(
            core_program_paths(
                read_core(core),
                core_file=core,
                project_root=root,
                files_root=core_files_root(core, root),
            )
        )
    config = resolve_checkout_project_dir(root) / "booley.toml"
    if config.is_file():
        paths.update(
            project_config_program_paths(
                tomllib.loads(config.read_text(encoding="utf-8")), project_root=root
            )
        )
    return frozenset(paths)
