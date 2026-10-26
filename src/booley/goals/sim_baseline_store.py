"""Independent, observational simulation baseline requests and contents-addressed results.

None of these documents participates in Goal acceptance or completion authority.
Entry captures nonversioned declarations before its final clean-tree check;
execution adapters schedule the requests after releasing lifecycle locks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import require_dict, require_list, require_str_value
from booley.criteria.evidence_ledger import canonical_json
from booley.flows.test_runs import TestRun, test_run_document, test_run_from_document
from booley.fusesoc.fusesoc_registry import discover_cores
from booley.goals.model import GoalFamily, GoalRecord, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.synth_baseline import (
    baseline_target,
    relative_synth_goals,
    synth_result_key,
    synth_units,
)
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.filesystem_utils import read_file_bytes

SCHEMA = "booley.sim-baseline/v1"
INPUTS_FILE = "sim-baseline-inputs.json"


def sim_goals(record: GoalRecord) -> tuple[RecordedGoal, ...]:
    """Only simulation Goals have baseline reports."""
    return tuple(goal for goal in record.goals if goal.spec.family is GoalFamily.SIM)


def sim_runs(_project: Path, record: GoalRecord, goal: RecordedGoal) -> tuple[TestRun, ...]:
    """The Test Runs a SIM Goal's baseline measures."""
    resolution = record.test_run_resolution(goal.spec.key)
    return resolution.test_runs if resolution else ()


def result_key(target: str, run: TestRun, stamp: str, protected_digest: str) -> dict[str, str]:
    """Contents-addressed evidence key; seeds and declared arguments are exact."""
    return {
        "target": target,
        "name": run.name,
        "fingerprint": run.fingerprint,
        "contents": stamp,
        "protected_inputs": protected_digest,
    }


def _goal_target(goal: RecordedGoal) -> str | None:
    """The Target a Goal's baseline measures unless its family names another."""
    return goal.spec.target


@dataclass(frozen=True)
class BaselineFamily:
    """One Goal family's baseline store adapter; the engine never names a family.

    ``name`` derives the on-disk layout (``goals/<name>-baselines``), the schema
    (``booley.<name>-baseline/v1``), Job ids (``<name>-baseline-<digest>``) and
    the Job endpoint (``<name>_baseline``). The executing half of an adapter
    lives in the MCP layer (``BaselineExecutor``), keyed by the same name.
    """

    name: str
    child_stem: str  # watchdog child documents: <stem>-request.json, <stem>-result.json
    freshness_key: str  # criterion key naming the dependent-content categories
    goals: Callable[[GoalRecord], tuple[RecordedGoal, ...]]
    #: ``(project, record, goal)`` -> the measured units of a Goal.
    runs: Callable[[Path, GoalRecord, RecordedGoal], tuple[Any, ...]]
    unit_document: Callable[[Any], dict[str, Any]]  # carries "name" and "fingerprint"
    parse_unit: Callable[[Mapping[str, Any]], Any]
    result_key: Callable[[str, Any, str, str], dict[str, str]]
    measured_target: Callable[[RecordedGoal], str | None] = _goal_target

    @property
    def schema(self) -> str:
        return f"booley.{self.name}-baseline/v1"

    @property
    def root_name(self) -> str:
        return f"{self.name}-baselines"

    @property
    def job_prefix(self) -> str:
        return f"{self.name}-baseline-"

    @property
    def endpoint(self) -> str:
        return f"{self.name}_baseline"


SIM_BASELINE = BaselineFamily(
    name="sim",
    child_stem="simulation",
    freshness_key="sim_pass_baseline",
    goals=sim_goals,
    runs=sim_runs,
    unit_document=test_run_document,
    parse_unit=test_run_from_document,
    result_key=result_key,
)
SYNTH_BASELINE = BaselineFamily(
    name="synth",
    child_stem="synthesis",
    freshness_key="synthesis_ok_baseline",
    goals=relative_synth_goals,
    runs=synth_units,
    unit_document=dict,
    parse_unit=dict,
    result_key=synth_result_key,
    measured_target=baseline_target,
)
#: Every family the baseline engine serves; read at call time.
BASELINE_FAMILIES: tuple[BaselineFamily, ...] = (SIM_BASELINE, SYNTH_BASELINE)


def family_for_schema(schema: object) -> BaselineFamily:
    """The registered family a baseline document's schema names."""
    for family in BASELINE_FAMILIES:
        if schema == family.schema:
            return family
    raise ValueError("unsupported simulation baseline schema")


def baseline_root(booley_dir: Path, family: BaselineFamily = SIM_BASELINE) -> Path:
    """Shared baseline requests, evidence and Jobs, outside Goal Record roots."""
    return booley_dir / "goals" / family.root_name


def baseline_jobs_dir(booley_dir: Path, family: BaselineFamily = SIM_BASELINE) -> Path:
    """Jobs that Finish must never read or fence."""
    return baseline_root(booley_dir, family) / ".runtime" / "jobs"


def content_digest(value: Mapping[str, Any]) -> str:
    """Stable digest without physical checkout or process identity."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def write_document(path: Path, value: Mapping[str, Any]) -> None:
    """Publish a complete private document atomically."""
    atomic_replace_bytes(path, canonical_json(dict(value)))


def read_document(path: Path, family: BaselineFamily | None = SIM_BASELINE) -> dict[str, Any]:
    """Validate the envelope at its filesystem boundary; ``None`` accepts any registered family."""
    raw = require_dict(json.loads(read_file_bytes(path)), field="simulation baseline")
    if family is None:
        family_for_schema(raw.get("schema"))
    elif raw.get("schema") != family.schema:
        raise ValueError("unsupported simulation baseline schema")
    return raw


def relative_path(value: object) -> Path:
    """Refuse traversal and absolute destinations in captured input names."""
    text = require_str_value(value, field="baseline input path")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or "\\" in text or ":" in text:
        raise ValueError("unsafe simulation baseline input path")
    return Path(path)


def capture_entry_inputs(booley_dir: Path, record: GoalRecord) -> None:
    """Capture declarations at entry; errors become report facts, never an entry gate."""
    path = record_paths(booley_dir, record.id).root / INPUTS_FILE
    root = Path(record.worktree_path).resolve()
    try:
        project = resolve_checkout_booley_dir(root)
        # Values are hex contents; ``None`` marks a protected non-file entry.
        files: dict[str, str | None] = {
            core.relative_to(root).as_posix(): core.read_bytes().hex()
            for core in discover_cores(root)
        }
        if record.project_snapshot is not None:
            files.update(
                {
                    (project.relative_to(root) / name).as_posix(): data
                    for name, data in record.project_snapshot.items()
                }
            )
        executable = {
            name: bool((root / name).stat().st_mode & 0o111)
            for name, data in files.items()
            if data is not None and (root / name).is_file()
        }
        directories: set[str] = set()
        _capture_protected(booley_dir, record, files, executable, directories)
        links = _core_links(root, project)
        write_document(
            path,
            {
                "schema": SCHEMA,
                "files": files,
                "links": links,
                "project_relative": project.relative_to(root).as_posix(),
                "executable": executable,
                "protected_directories": sorted(directories),
            },
        )
    except (OSError, ValueError, RuntimeError) as exc:
        write_document(path, {"schema": SCHEMA, "error": f"entry inputs unavailable: {exc}"})


def _capture_protected(
    booley_dir: Path,
    record: GoalRecord,
    files: dict[str, str | None],
    executable: dict[str, bool],
    directories: set[str],
) -> None:
    """Retain checkout-local protected programs as well as Project configuration."""
    from booley.goals.protected_inputs import ProtectedInputRoots, ProtectedPath

    root = Path(record.worktree_path).resolve()
    project = resolve_checkout_booley_dir(root)
    roots = ProtectedInputRoots(root, booley_dir)
    main = roots.main_checkout()
    paths = sorted(record.protected_paths, key=lambda value: not value.startswith("worktree:"))
    for encoded in paths:
        physical = roots.absolute(ProtectedPath.decode(encoded), main)
        if physical is None:
            continue
        if physical.is_relative_to(root):
            relative = physical.relative_to(root)
        elif physical.is_relative_to(booley_dir):
            relative = project.relative_to(root) / physical.relative_to(booley_dir)
        else:
            continue  # outside-checkout inputs cannot be copied into a baseline dependency
        if physical.is_dir():
            directories.add(relative.as_posix())
        leaves = tuple(physical.rglob("*")) if physical.is_dir() else (physical,)
        for leaf in leaves:
            if "__pycache__" in leaf.parts or leaf.is_dir():
                continue
            name = (
                (relative / leaf.relative_to(physical)).as_posix()
                if physical.is_dir()
                else relative.as_posix()
            )
            if leaf.is_symlink():
                raise ValueError("protected baseline input crosses a symbolic link")
            if name not in files:
                files[name] = leaf.read_bytes().hex() if leaf.is_file() else None
                if leaf.is_file():
                    executable[name] = bool(leaf.stat().st_mode & 0o111)


def _core_links(root: Path, project: Path) -> dict[str, str]:
    """Capture only contained resolution links, with checkout-relative destinations."""
    links: dict[str, str] = {}
    cores = project / "cores"
    for path in cores.rglob("*") if cores.is_dir() else ():
        if path.is_symlink():
            destination = path.resolve().relative_to(root.resolve())
            links[path.relative_to(root).as_posix()] = destination.as_posix()
    return links


def entry_inputs(booley_dir: Path, record: GoalRecord) -> dict[str, Any]:
    """The same independently captured inputs across entry, changes and lazy resolution."""
    return read_document(record_paths(booley_dir, record.id).root / INPUTS_FILE)


def request_identity(
    project: Path,
    record: GoalRecord,
    goal: RecordedGoal,
    inputs: Mapping[str, Any],
    family: BaselineFamily = SIM_BASELINE,
) -> dict[str, Any]:
    """Index one base selection; a second session needs no checkout or simulation Job."""
    return {
        "base_sha": record.base_sha,
        "paired_base_sha": record.paired_project_base_sha,
        "protected_digest": record.protected_digest,
        "target": family.measured_target(goal),
        "inputs_digest": content_digest(inputs),
        "runs": [family.unit_document(unit) for unit in family.runs(project, record, goal)],
    }


def request_path(
    booley_dir: Path, identity: Mapping[str, Any], family: BaselineFamily = SIM_BASELINE
) -> Path:
    """Stable shared request directory, confined by a canonical digest."""
    return (
        baseline_root(booley_dir, family) / "requests" / content_digest(identity) / "request.json"
    )


def cache_path(
    booley_dir: Path, key: Mapping[str, str], family: BaselineFamily = SIM_BASELINE
) -> Path:
    """The per-unit evidence document."""
    return baseline_root(booley_dir, family) / "evidence" / (content_digest(key) + ".json")


def cached_result(
    booley_dir: Path, key: Mapping[str, str], family: BaselineFamily = SIM_BASELINE
) -> dict[str, Any] | None:
    """A validated successful observation; unavailable outcomes are never cached."""
    path = cache_path(booley_dir, key, family)
    if not path.exists():
        return None
    raw = read_document(path, family)
    if raw.get("key") != dict(key) or raw.get("grade") not in {"pass", "fail"}:
        raise ValueError("simulation baseline cache identity or grade differs")
    proof = require_dict(raw.get("proof"), field="baseline source proof")
    if (
        proof.get("contents") != key["contents"]
        or proof.get("protected_digest") != key["protected_inputs"]
        or proof.get("target_identity") != key["target"]
    ):
        raise ValueError("simulation baseline cache source proof differs")
    return raw


def request_runs(raw: Mapping[str, Any]) -> tuple[TestRun, ...]:
    """Exact resolved Test Runs of a validated baseline request."""
    identity = require_dict(raw.get("identity"), field="baseline request identity")
    return tuple(
        test_run_from_document(row) for row in require_list(identity.get("runs"), field="runs")
    )


def validate_results(
    project: Path, request: Mapping[str, Any], family: BaselineFamily = SIM_BASELINE
) -> None:
    """Corrupt report evidence is unavailable, never a status or Finish failure."""
    status = request.get("status")
    if status not in {"pending", "complete", "unavailable"}:
        raise ValueError("invalid simulation baseline request status")
    identity = require_dict(request.get("identity"), field="baseline request identity")
    wanted: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
    for raw_unit in require_list(identity.get("runs"), field="runs"):
        unit = family.parse_unit(raw_unit)
        document = family.unit_document(unit)
        wanted[(document["name"], document["fingerprint"])] = (unit, document)
    rows = require_list(request.get("results"), field="baseline results")
    found: set[tuple[str, str]] = set()
    for raw in rows:
        row = require_dict(raw, field="baseline result")
        pair = (
            require_str_value(row.get("name"), field="baseline result name"),
            require_str_value(row.get("fingerprint"), field="baseline result fingerprint"),
        )
        if pair not in wanted or pair in found:
            raise ValueError("baseline result is not an exact requested Test Run")
        unit, document = wanted[pair]
        if any(row.get(key) != value for key, value in document.items()):
            raise ValueError("baseline result Test Run inputs differ")
        grade = row.get("grade")
        if grade not in {"pass", "fail", "new", "not comparable", "unavailable"}:
            raise ValueError("invalid baseline result grade")
        proof = require_dict(row.get("proof"), field="baseline result proof")
        require_str_value(proof.get("contents"), field="baseline result contents")
        if proof.get("protected_digest") != request["identity"]["protected_digest"]:
            raise ValueError("baseline result protected inputs differ from entry")
        if (
            grade in {"new", "not comparable"}
            and proof.get("base_sha") != request["identity"]["base_sha"]
        ):
            raise ValueError("baseline absence or incompatibility proof names another base")
        if grade in {"pass", "fail"}:
            key = family.result_key(
                require_str_value(proof.get("target_identity"), field="baseline Target identity"),
                unit,
                proof["contents"],
                require_str_value(
                    proof.get("protected_digest"), field="baseline protected inputs"
                ),
            )
            cached = cached_result(project, key, family)
            if (
                cached is None
                or cached["grade"] != grade
                or cached["proof"] != proof
                or cached.get("measurement") != row.get("measurement")
            ):
                raise ValueError("baseline report differs from its contents-addressed evidence")
        found.add(pair)
    if status == "complete" and found != set(wanted):
        raise ValueError("completed baseline report is missing Test Runs")
