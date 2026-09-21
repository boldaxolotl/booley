"""Translate one resolved ordinary-HDL Target into a campaign plan."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from booley.flows.sim.config import (
    parse_run_cwd_template,
    resolve_pre_sim_build_access,
    resolve_pre_sim_commands,
    resolve_run_cwd,
)
from booley.flows.sim.execution.contract import SimulationPreview
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection

from .codec import SimulationCampaignIntegrityError
from .model import SimulationCampaignPlan, create_simulation_campaign_plan
from .planning import (
    canonical_sha256,
    derive_runtime_input_declarations,
    finalize_manifest,
)


def plan_ordinary_hdl_campaign(
    *,
    handle: TargetHandle,
    inspection: TargetInspection,
    preview: SimulationPreview,
    groups: Sequence[tuple[str, ...]],
    required_suite: Sequence[str],
    revision: str,
    invocation_id: int,
    execution_id: str,
    trace: bool,
    prerequisite_documents: Sequence[Mapping[str, object]] = (),
    planning_disclosures: Sequence[Mapping[str, object]] = (),
    role: str = "candidate",
) -> SimulationCampaignPlan:
    """Build one strict immutable plan from already-resolved Target facts."""
    if inspection.handle != handle or preview.target_identity != handle.identity:
        raise SimulationCampaignIntegrityError("campaign planning Target facts disagree")
    if inspection.flow_options.get("cocotb_module"):
        raise SimulationCampaignIntegrityError("ordinary HDL planner cannot plan Cocotb")
    if role not in {"candidate", "cycle_count_baseline"}:
        raise SimulationCampaignIntegrityError("campaign planning role is invalid")
    if not revision:
        raise SimulationCampaignIntegrityError("campaign revision is required")
    root = handle.project_root.resolve()
    sources = _source_entries(root, inspection.inputs)
    if len(groups) != len(preview.commands):
        raise SimulationCampaignIntegrityError("planned groups and commands disagree")
    workload, configured_cwd, run_kind, sources = _workload_document(
        handle, inspection, sources, trace
    )
    target = _target_document(handle, root, revision, role)
    suite = _required_suite(root, required_suite)
    variant = _variant_document(workload, sources, trace)
    work_items = _work_items(groups, target, variant, configured_cwd, run_kind)
    manifest = _manifest_document(
        invocation_id, execution_id, target, workload, suite, variant,
        planning_disclosures, prerequisite_documents, work_items,
    )
    return create_simulation_campaign_plan(manifest)


def _manifest_document(
    invocation_id, execution_id, target, workload, suite, variant,
    planning_disclosures, prerequisite_documents, work_items,
):
    return finalize_manifest({
        "$schema": "booley.simulation-campaign-manifest/v1",
        "campaign_id": str(uuid.uuid4()),
        "created_at": utc_now_rfc3339(),
        "origin": {"execution_id": execution_id, "invocation_id": invocation_id},
        "target": target, "workload": workload, "required_suite": suite,
        "build_variants": [variant],
        "planning_disclosures": list(planning_disclosures),
        "prerequisites": list(prerequisite_documents), "work_items": work_items,
    })


def _workload_document(
    handle: TargetHandle,
    inspection: TargetInspection,
    sources: list[dict[str, object]],
    trace: bool,
) -> tuple[dict[str, object], str, str, list[dict[str, object]]]:
    root = handle.project_root.resolve()
    parameters = [
        {"name": name, "value": value}
        for name, value in sorted(inspection.parameters.items())
    ]
    source_recipe = {
        "sources": sources,
        "parameters": parameters,
        "defines": _defines(inspection.flow_options),
        "pre_sim_commands": list(resolve_pre_sim_commands(root)),
    }
    command_model = {
        "target_identity": handle.identity, "toplevel": inspection.toplevel,
        "eda_tool": inspection.eda_tool or "", "parameters": parameters,
        "flow_options": dict(inspection.flow_options), "trace": trace,
    }
    build_recipe = {
        "backend": inspection.eda_tool or "", "toplevel": inspection.toplevel,
        "arguments": [], "command_model_sha256": canonical_sha256(command_model),
    }
    configured_cwd = resolve_run_cwd(root)
    placeholders = parse_run_cwd_template(configured_cwd)
    run_kind = "templated" if placeholders else "literal"
    return {
        "mode": "simulate", "trace": trace, "coverage": False,
        "eda": {"kind": inspection.eda_tool or "", "version": _eda_identity(inspection.eda_tool)},
        "planner_contract_version": "1", "adapter_contract_version": "1",
        "pre_sim_build_access": resolve_pre_sim_build_access(root),
        "run_cwd": {"configured": configured_cwd, "kind": run_kind,
                    "placeholders": list(placeholders)},
        "runtime_inputs": list(derive_runtime_input_declarations(inspection.inputs)),
        "source_recipe": source_recipe, "build_recipe": build_recipe,
    }, configured_cwd, run_kind, sources


def _target_document(
    handle: TargetHandle, root: Path, revision: str, role: str
) -> dict[str, object]:
    return {
        "vlnv": handle.vlnv, "name": handle.name, "selector": handle.selector,
        "project_identity": canonical_sha256(str(root)), "revision": revision,
        "role": role, "display_name": handle.selector,
    }


def _variant_document(
    workload: Mapping[str, object], sources: list[dict[str, object]], trace: bool
) -> dict[str, object]:
    recipe = {
        "kind": "trace" if trace else "candidate", "source_closure": sources,
        "source_recipe": workload["source_recipe"],
        "build_recipe": workload["build_recipe"], "eda": workload["eda"],
        "trace": trace, "coverage": False,
    }
    digest = canonical_sha256(recipe)
    return {
        "build_variant_id": "variant:" + digest.removeprefix("sha256:"),
        "kind": recipe["kind"], "sharing_eligible": True,
        "source_closure": sources, "recipe_sha256": digest,
    }


def _source_entries(root: Path, inputs: Sequence[TargetInput]) -> list[dict[str, object]]:
    entries = []
    for item in inputs:
        path = Path(item.path)
        resolved = (path if path.is_absolute() else root / path).resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
            raw = resolved.read_bytes()
        except ValueError as exc:
            raise SimulationCampaignIntegrityError(
                f"Target source escapes Project root: {item.path}"
            ) from exc
        except OSError as exc:
            raise SimulationCampaignIntegrityError(
                f"Target source is unavailable: {item.path}: {exc}"
            ) from exc
        kind = (
            "user"
            if item.file_type.lower() == "user"
            else "constraint"
            if item.file_type.lower() == "sdc"
            else "testbench"
            if "tb" in item.tags
            else "rtl"
        )
        entries.append(
            {
                "path": relative,
                "bytes": len(raw),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "kind": kind,
            }
        )
    return entries


def _defines(options: Mapping[str, object]) -> list[str]:
    raw = options.get("defines", ())
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Sequence):
        return [str(item) for item in raw]
    return []


def _eda_identity(kind: str | None) -> str:
    import shutil

    executable = shutil.which("iverilog" if kind in {"icarus", "iverilog"} else kind or "")
    if executable is None:
        return "unavailable"
    try:
        path = Path(executable).resolve(strict=True)
        raw = path.read_bytes()
    except OSError:
        return "unavailable"
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _required_suite(root: Path, names: Sequence[str]) -> dict[str, object]:
    ordered = list(dict.fromkeys(names))
    if not ordered:
        raw = b""
        relative = ""
    else:
        path = root / ".booley_project" / "tests.toml"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SimulationCampaignIntegrityError(
                "registered Required Simulation Suite has no readable tests.toml"
            ) from exc
        relative = path.relative_to(root).as_posix()
    return {
        "names": ordered,
        "default_invocation": not ordered,
        "source_path": relative,
        "source_bytes": len(raw),
        "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _work_items(
    groups: Sequence[tuple[str, ...]],
    target: Mapping[str, object],
    variant: Mapping[str, object],
    configured_cwd: str,
    run_kind: str,
) -> list[dict[str, object]]:
    items = []
    collision = _collision_template(configured_cwd)
    for ordinal, names in enumerate(groups):
        if len(names) > 1:
            raise SimulationCampaignIntegrityError("ordinary HDL work item has multiple tests")
        identity = {
            "ordinal": ordinal,
            "kind": "ordinary_hdl",
            "role": target["role"],
            "revision": target["revision"],
            "target": target,
            "selection": {
                "kind": "named" if names else "default",
                "names": list(names),
            },
            "arguments": list(names),
            "build_variant_id": variant["build_variant_id"],
            "run_directory": {
                "configured": configured_cwd,
                "kind": run_kind,
                "collision_template": collision,
            },
        }
        fingerprint = canonical_sha256(identity)
        items.append(
            {
                "work_item_id": (
                    f"item:{ordinal:04d}:" + fingerprint.removeprefix("sha256:")[:16]
                ),
                **identity,
                "fingerprint_sha256": fingerprint,
            }
        )
    return items


def _collision_template(configured: str) -> str:
    rendered = configured
    for field in ("campaign", "target", "test", "attempt"):
        rendered = rendered.replace("{" + field + "}", field)
    return str(Path(rendered).absolute()) if Path(rendered).is_absolute() else rendered


__all__ = ["plan_ordinary_hdl_campaign"]
