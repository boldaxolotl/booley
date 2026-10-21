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
from booley.flows.test_runs import TestRun, test_run_document
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection

from .codec import MANIFEST_SCHEMA, SimulationCampaignIntegrityError
from .model import SimulationCampaignPlan, create_simulation_campaign_plan
from .planning import (
    canonical_sha256,
    derive_runtime_input_declarations,
    finalize_manifest,
)


def _require_ordinary_hdl_inputs(
    handle: TargetHandle,
    inspection: TargetInspection,
    preview: SimulationPreview,
    role: str,
    revision: str,
) -> None:
    """Refuse Target facts the ordinary-HDL planner cannot plan from."""
    if inspection.handle != handle or preview.target_identity != handle.identity:
        raise SimulationCampaignIntegrityError("campaign planning Target facts disagree")
    if inspection.flow_options.get("cocotb_module"):
        raise SimulationCampaignIntegrityError("ordinary HDL planner cannot plan Cocotb")
    if role not in {"candidate", "cycle_count_baseline"}:
        raise SimulationCampaignIntegrityError("campaign planning role is invalid")
    if not revision:
        raise SimulationCampaignIntegrityError("campaign revision is required")


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
    required_suite_catalog_backed: bool | None = None,
) -> SimulationCampaignPlan:
    """Build one strict immutable plan from already-resolved Target facts."""
    _require_ordinary_hdl_inputs(handle, inspection, preview, role, revision)
    root = handle.project_root.resolve()
    sources = _source_entries(root, inspection.inputs)
    if len(groups) != len(preview.commands):
        raise SimulationCampaignIntegrityError("planned groups and commands disagree")
    run_groups = preview.run_groups
    _require_matching_run_groups(groups, run_groups)
    workload, configured_cwd, run_kind, sources = _workload_document(
        handle, inspection, sources, trace, coverage=False
    )
    target = _target_document(handle, root, revision, role)
    suite = _required_suite(root, required_suite, catalog_backed=required_suite_catalog_backed)
    variant = _variant_document(workload, sources, trace)
    work_items = _work_items(
        run_groups, target, variant, configured_cwd, run_kind, kind="ordinary_hdl"
    )
    manifest = _manifest_document(
        invocation_id,
        execution_id,
        target,
        workload,
        suite,
        variant,
        planning_disclosures,
        prerequisite_documents,
        work_items,
    )
    return create_simulation_campaign_plan(manifest)


def plan_coarse_simulation_campaign(
    *,
    handle: TargetHandle,
    inspection: TargetInspection,
    preview: SimulationPreview,
    selected_tests: Sequence[str],
    required_suite: Sequence[str],
    revision: str,
    invocation_id: int,
    execution_id: str,
    trace: bool,
    kind: str,
    planning_disclosures: Sequence[Mapping[str, object]] = (),
    required_suite_catalog_backed: bool | None = None,
    no_waivers: bool = False,
) -> SimulationCampaignPlan:
    """Plan honest coarse work: Cocotb batches, or one native coverage aggregate.

    ``preview.run_groups`` holds each work item's Test Runs: one group per Cocotb
    batch (shared environment and seed), or the one group a coverage aggregate
    collects. Only a coverage aggregate may freeze ``no_waivers`` into its workload.
    """
    _validate_coarse_inputs(handle, inspection, preview, selected_tests, kind, no_waivers)
    runs = preview.run_groups
    root = handle.project_root.resolve()
    sources = _source_entries(root, inspection.inputs)
    workload, configured_cwd, run_kind, sources = _workload_document(
        handle,
        inspection,
        sources,
        trace,
        coverage=kind == "coverage_aggregate",
        no_waivers=no_waivers,
    )
    target = _target_document(handle, root, revision, "candidate")
    suite = _required_suite(root, required_suite, catalog_backed=required_suite_catalog_backed)
    variant = _variant_document(workload, sources, trace)
    work_items = _work_items(runs, target, variant, configured_cwd, run_kind, kind=kind)
    manifest = _manifest_document(
        invocation_id,
        execution_id,
        target,
        workload,
        suite,
        variant,
        planning_disclosures,
        (),
        work_items,
    )
    return create_simulation_campaign_plan(manifest)


def _validate_coarse_inputs(
    handle: TargetHandle,
    inspection: TargetInspection,
    preview: SimulationPreview,
    selected_tests: Sequence[str],
    kind: str,
    no_waivers: bool,
) -> None:
    run_groups = preview.run_groups
    if kind not in {"cocotb_batch", "coverage_aggregate"}:
        raise SimulationCampaignIntegrityError("coarse campaign kind is invalid")
    if no_waivers and kind != "coverage_aggregate":
        raise SimulationCampaignIntegrityError("no_waivers requires a coverage aggregate")
    if inspection.handle != handle or preview.target_identity != handle.identity:
        raise SimulationCampaignIntegrityError("campaign planning Target facts disagree")
    is_cocotb = bool(inspection.flow_options.get("cocotb_module"))
    if kind == "cocotb_batch" and not is_cocotb:
        raise SimulationCampaignIntegrityError("coarse campaign kind disagrees with Target")
    names = tuple(selected_tests)
    planned = tuple(_selection_names(group) for group in run_groups)
    if kind == "coverage_aggregate":
        # One aggregate collects every selected Test Run, however many of one Test.
        if len(run_groups) != 1 or planned != (names,) or not names:
            raise SimulationCampaignIntegrityError(
                "coverage aggregate requires one group of named Test Runs"
            )
    elif preview.groups != planned or tuple(name for group in planned for name in group) != names:
        raise SimulationCampaignIntegrityError(
            "coarse campaign selection disagrees with execution preview"
        )
    run_names = [run.name for group in run_groups for run in group]
    if len(set(run_names)) != len(run_names):
        raise SimulationCampaignIntegrityError("coarse campaign Test Runs must be unique")


def _manifest_document(
    invocation_id,
    execution_id,
    target,
    workload,
    suite,
    variant,
    planning_disclosures,
    prerequisite_documents,
    work_items,
):
    # v4: every work item records its concrete Test Runs (ADR 0072). Older
    # manifests name Tests only; they stay readable but are never written.
    return finalize_manifest(
        {
            "$schema": MANIFEST_SCHEMA,
            "campaign_id": str(uuid.uuid4()),
            "created_at": utc_now_rfc3339(),
            "origin": {"execution_id": execution_id, "invocation_id": invocation_id},
            "target": target,
            "workload": workload,
            "required_suite": suite,
            "build_variants": [variant],
            "planning_disclosures": list(planning_disclosures),
            "prerequisites": list(prerequisite_documents),
            "work_items": work_items,
        }
    )


def _recipes(
    handle: TargetHandle,
    inspection: TargetInspection,
    sources: list[dict[str, object]],
    trace: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build the frozen source and build recipes of a Target workload."""
    root = handle.project_root.resolve()
    parameters = [
        {"name": name, "value": value} for name, value in sorted(inspection.parameters.items())
    ]
    source_recipe = {
        "sources": sources,
        "parameters": parameters,
        "defines": _defines(inspection.flow_options),
        "pre_sim_commands": list(resolve_pre_sim_commands(root)),
    }
    command_model = {
        "target_identity": handle.identity,
        "toplevel": inspection.toplevel,
        "eda_tool": inspection.eda_tool or "",
        "parameters": parameters,
        "flow_options": dict(inspection.flow_options),
        "trace": trace,
    }
    build_recipe = {
        "eda_tool": inspection.eda_tool or "",
        "toplevel": inspection.toplevel,
        "arguments": [],
        "command_model_sha256": canonical_sha256(command_model),
    }
    return source_recipe, build_recipe


def _workload_document(
    handle: TargetHandle,
    inspection: TargetInspection,
    sources: list[dict[str, object]],
    trace: bool,
    *,
    coverage: bool,
    no_waivers: bool = False,
) -> tuple[dict[str, object], str, str, list[dict[str, object]]]:
    root = handle.project_root.resolve()
    source_recipe, build_recipe = _recipes(handle, inspection, sources, trace)
    configured_cwd = resolve_run_cwd(root)
    placeholders = parse_run_cwd_template(configured_cwd)
    run_kind = "templated" if placeholders else "literal"
    workload = {
        "mode": "simulate",
        "trace": trace,
        "coverage": coverage,
        "eda": {
            "kind": inspection.eda_tool or "",
            "version": _eda_identity(inspection.eda_tool),
        },
        "planner_contract_version": "1",
        "adapter_contract_version": "1",
        "pre_sim_build_access": resolve_pre_sim_build_access(root),
        "run_cwd": {
            "configured": configured_cwd,
            "kind": run_kind,
            "placeholders": list(placeholders),
        },
        "runtime_inputs": list(derive_runtime_input_declarations(inspection.inputs)),
        "source_recipe": source_recipe,
        "build_recipe": build_recipe,
    }
    if no_waivers:
        # Written only when requested and never as ``false``: absence means waivers apply.
        workload["no_waivers"] = True
    return workload, configured_cwd, run_kind, sources


def _target_document(
    handle: TargetHandle, root: Path, revision: str, role: str
) -> dict[str, object]:
    return {
        "vlnv": handle.vlnv,
        "name": handle.name,
        "selector": handle.selector,
        "project_identity": canonical_sha256(str(root)),
        "revision": revision,
        "role": role,
        "display_name": handle.selector,
    }


def _variant_document(
    workload: Mapping[str, object], sources: list[dict[str, object]], trace: bool
) -> dict[str, object]:
    kind = "coverage" if workload["coverage"] is True else "trace" if trace else "candidate"
    recipe = {
        "kind": kind,
        "source_closure": sources,
        "source_recipe": workload["source_recipe"],
        "build_recipe": workload["build_recipe"],
        "eda": workload["eda"],
        "trace": trace,
        "coverage": workload["coverage"],
    }
    digest = canonical_sha256(recipe)
    return {
        "build_variant_id": "variant:" + digest.removeprefix("sha256:"),
        "kind": recipe["kind"],
        "sharing_eligible": workload["pre_sim_build_access"] == "immutable",
        "source_closure": sources,
        "recipe_sha256": digest,
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
                f"Target source escapes Project Root: {item.path}"
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


def _required_suite(
    root: Path,
    names: Sequence[str],
    *,
    catalog_backed: bool | None = None,
) -> dict[str, object]:
    ordered = list(dict.fromkeys(names))
    backed = bool(ordered) if catalog_backed is None else catalog_backed
    if not backed:
        raw = b""
        relative = ""
    else:
        path = root / ".booley" / "tests.toml"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SimulationCampaignIntegrityError(
                "registered Required Simulation Suite has no readable tests.toml"
            ) from exc
        relative = path.relative_to(root).as_posix()
    return {
        "names": ordered,
        "default_invocation": not backed,
        "source_path": relative,
        "source_bytes": len(raw),
        "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _require_matching_run_groups(
    groups: Sequence[tuple[str, ...]], run_groups: Sequence[tuple[TestRun, ...]]
) -> None:
    """The Test Runs of each group must be the group's Tests, in order."""
    if tuple(groups) != tuple(_selection_names(runs) for runs in run_groups):
        raise SimulationCampaignIntegrityError("planned groups and Test Runs disagree")


def _selection_names(runs: Sequence[TestRun]) -> tuple[str, ...]:
    """Bare Test names of a work item, each once, in first-run order."""
    return tuple(dict.fromkeys(run.test for run in runs if run.test is not None))


def _work_items(
    run_groups: Sequence[tuple[TestRun, ...]],
    target: Mapping[str, object],
    variant: Mapping[str, object],
    configured_cwd: str,
    run_kind: str,
    *,
    kind: str,
) -> list[dict[str, object]]:
    items = []
    collision = _collision_template(configured_cwd)
    for ordinal, runs in enumerate(run_groups):
        names = _selection_names(runs)
        if kind == "ordinary_hdl" and len(runs) != 1:
            raise SimulationCampaignIntegrityError(
                "ordinary HDL work item requires exactly one Test Run"
            )
        selection_kind = "named" if names else "default"
        if kind == "cocotb_batch" and not names:
            selection_kind = "unfiltered"
        identity = {
            "ordinal": ordinal,
            "kind": kind,
            "role": target["role"],
            "revision": target["revision"],
            "target": target,
            "selection": {
                "kind": selection_kind,
                "names": list(names),
            },
            "arguments": list(names),
            "test_runs": [test_run_document(run) for run in runs],
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


__all__ = ["plan_coarse_simulation_campaign", "plan_ordinary_hdl_campaign"]
