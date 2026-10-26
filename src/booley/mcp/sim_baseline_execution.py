"""Execute independent SIM baseline observations using the existing baseline checkout.

A detached supervisor owns the checkout. A bounded child runs the ordinary
Simulation Flow with a standalone, in-memory adapter: no Goal state, ledger
or campaign registration is reachable from it.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.booley_dir_links import is_booley_dir_entry
from booley.core.boundary import require_dict, require_list, require_str_value
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows.baseline_worktree import baseline_worktree, baseline_worktree_path
from booley.flows.campaign_results import StartedCampaign
from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.frozen_test_runs import current_declaration
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.test_runs import TestRun, test_run_document
from booley.fusesoc.fusesoc_registry import prepare_core_library_plan
from booley.goals.checkout import GoalCheckout
from booley.goals.committed_export import pinned_working_bytes, tracked_matches_pin
from booley.goals.freshness import DEFAULT_RESOLVERS, goal_required_categories
from booley.goals.input_identity import main_booley_dir_entry
from booley.goals.model import GoalRecord
from booley.goals.sim_baseline_store import (
    SCHEMA,
    BaselineFamily,
    baseline_root,
    cache_path,
    cached_result,
    content_digest,
    family_for_schema,
    read_document,
    relative_path,
    request_runs,
    write_document,
)
from booley.goals.test_run_evidence import freshness_stamp
from booley.goals.test_run_freezing import TargetPlans
from booley.mcp.synth_baseline_execution import classify_synth, measure_synth, synth_job_class
from booley.runtime import job_slots
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.git_ignore import ignored_paths
from booley.runtime.process_group import (
    ProcessGroup,
    capture_process_group,
    new_group_kwargs,
    request_group_termination,
    terminate_process_group,
)

SIM_KEY = "sim_pass_baseline"
# Digest of the canonical empty file list, for declaration-only fingerprint categories.
EMPTY_CATEGORY_DIGEST = hashlib.sha256(b"[]").hexdigest()
Execute = Callable[[Path, str, tuple[TestRun, ...], Path], dict[str, Any]]


@dataclass(frozen=True)
class BaselineExecutor:
    """The executing half of a family adapter (its store half is ``BaselineFamily``)."""

    #: ``(checkout, request, units)`` -> ``(Target identity, graded rows, runnable units)``.
    classify: Callable[
        [Path, Mapping[str, Any], tuple[Any, ...]],
        tuple[str | None, list[dict[str, Any]], tuple[Any, ...]],
    ]
    #: Runs inside the watchdog child; returns ``{"rows", "stamps", "reason", ...}``.
    measure: Callable[[Mapping[str, Any]], dict[str, Any]]
    #: The Job Class of the Flow that ``measure`` runs; the supervisor claims it.
    job_class: Callable[[], str]


def contents_stamp(fingerprint: Mapping[str, Any], key: str = SIM_KEY) -> str:
    """Use the same dependent-content categories as candidate evidence for *key*."""
    stamp = freshness_stamp(key, {"_source_fingerprint": {"fingerprint": fingerprint}})
    if stamp is None:
        raise ValueError("baseline dependent contents cannot be fingerprinted")
    return stamp


def restore_entry_inputs(root: Path, request: Mapping[str, Any]) -> None:
    """Restore captured configuration/cores, never a new testbench or live declaration."""
    inputs = require_dict(request["inputs"], field="baseline entry inputs")
    if inputs.get("error"):
        raise ValueError(inputs["error"])
    files = require_dict(inputs.get("files"), field="baseline input files")
    links = require_dict(inputs.get("links"), field="baseline resolution links")
    record = GoalRecord.from_json(request["record"])
    project = root / relative_path(inputs["project_relative"])
    project.mkdir(parents=True, exist_ok=True)
    if record.project_snapshot is not None and (project / "cores").is_dir():
        shutil.rmtree(project / "cores")
    for name, data in files.items():
        path = root / relative_path(name)
        if path.is_symlink() or any(
            parent.is_symlink() and not is_booley_dir_entry(parent, root.parent)
            for parent in path.parents
            if parent != root
        ):
            raise ValueError("baseline input destination crosses a link")
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes.fromhex(require_str_value(data, field="baseline input bytes")))
    for name, target in links.items():
        path, destination = root / relative_path(name), root / relative_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(
            os.path.relpath(destination, path.parent), target_is_directory=destination.is_dir()
        )
    executable = require_dict(inputs.get("executable", {}), field="baseline executable inputs")
    for name, enabled in executable.items():
        path = root / relative_path(name)
        if type(enabled) is not bool:
            raise ValueError("baseline executable flag must be boolean")
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111 if enabled else path.stat().st_mode & ~0o111)
    prepare_core_library_plan(root)


def prove_base_contents(
    root: Path, request: Mapping[str, Any], fingerprint: Mapping[str, Any]
) -> dict[str, Any]:
    """Check dependency bytes against pinned Git objects or entry configuration bytes."""
    record = GoalRecord.from_json(request["record"])
    key = family_for_schema(request["schema"]).freshness_key
    if GoalCheckout(root).head_sha() != record.base_sha:
        raise ValueError("baseline checkout is not at the Goal base commit")
    captured = require_dict(request["inputs"]["files"], field="entry input files")
    projections: dict[Path, Any] = {}
    proof: dict[str, str] = {}
    for name in _dependent_files(fingerprint, key):
        relative = relative_path(name)
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"baseline dependency escapes checkout: {name}")
        found = GoalCheckout(path.parent).containing_repository()
        if found is None:
            raise ValueError(f"baseline dependency has no pinned repository: {name}")
        repository = found[0]
        pin = _participant_pin(repository, root.resolve(), record)
        if repository not in projections:
            projections[repository] = pinned_working_bytes(repository, pin)
        projection = projections[repository]
        if tracked_matches_pin(repository, pin, path.resolve(), projection=projection):
            proof[str(name)] = pin
        elif (
            str(name) in captured
            and captured[str(name)] is not None
            and path.read_bytes().hex() == captured[str(name)]
        ):
            # Only unversioned configuration can use the independently captured entry bytes.
            if os.fsencode(path.resolve().relative_to(repository).as_posix()) in projection.rows:
                raise ValueError(f"versioned baseline dependency differs from base: {name}")
            proof[str(name)] = "entry-snapshot"
        else:
            raise ValueError(f"baseline dependency differs from base or is unavailable: {name}")
    _check_captured_inputs(root, request)
    return {
        "base_sha": record.base_sha,
        "protected_digest": record.protected_digest,
        "paired_base_sha": record.paired_project_base_sha,
        "contents": contents_stamp(fingerprint, key),
        "dependencies": proof,
    }


def _dependent_files(fingerprint: Mapping[str, Any], key: str) -> set[str]:
    names: set[str] = set()
    for category in goal_required_categories(key):
        group = require_dict(fingerprint[category], field="baseline fingerprint category")
        names.update(
            require_str_value(name, field="baseline dependency")
            for name in require_list(group.get("files"), field="baseline fingerprint files")
        )
    return names


def _check_captured_inputs(root: Path, request: Mapping[str, Any]) -> None:
    for name, data in request["inputs"]["files"].items():
        path = root / relative_path(name)
        actual = path.read_bytes().hex() if path.is_file() else None
        if actual != data:
            raise ValueError(f"baseline entry input changed during execution: {name}")
        executable = request["inputs"]["executable"].get(name)
        if (
            os.name != "nt"
            and executable is not None
            and bool(path.stat().st_mode & 0o111) != executable
        ):
            raise ValueError(f"baseline input executable state changed: {name}")
    for relative in request["inputs"].get("protected_directories", []):
        directory = root / relative_path(relative)
        expected = {
            name
            for name, data in request["inputs"]["files"].items()
            if data is not None and Path(name).is_relative_to(relative_path(relative))
        }
        actual = {
            path.relative_to(root).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if actual != expected:
            raise ValueError(f"baseline protected directory changed: {relative}")


def _participant_pin(repository: Path, root: Path, record: GoalRecord) -> str:
    from booley.runtime.pinned_history import raw_git

    pin = GoalCheckout(repository).head_sha()
    if repository == root:
        return record.base_sha
    if repository == resolve_checkout_booley_dir(root):
        if record.paired_project_base_sha != pin:
            raise ValueError("baseline paired Project commit differs")
        return pin
    parent = GoalCheckout(repository.parent).containing_repository()
    if parent is None or not parent[0].is_relative_to(root):
        raise ValueError("baseline dependency has an unpinned nested repository")
    parent_pin = _participant_pin(parent[0], root, record)
    relative = repository.relative_to(parent[0]).as_posix()
    entry = raw_git(parent[0], "ls-tree", parent_pin, "--", relative)
    if not entry.startswith(f"160000 commit {pin}\t".encode()):
        raise ValueError("baseline submodule does not match its base gitlink")
    return pin


def baseline_kind(plan: TargetTestPlan | None, run: TestRun) -> tuple[str, str]:
    """Absence is new; an incompatible resolved Test Run is never a transition."""
    if plan is None:
        return "new", "Target absent at baseline"
    if plan.adapter == "cocotb" and not plan.tests:
        return "unavailable", "baseline cocotb Test discovery is not registered"
    if run.test is not None and run.test not in plan.tests:
        return "new", "Test absent at baseline"
    if current_declaration(plan, run) != run:
        return "not comparable", "baseline Variant, seed or declared arguments differ"
    return "runnable", ""


def _ignored_destination(root: Path, base: str) -> None:
    scratch = baseline_worktree_path(root, base)
    state_dir = scratch.parent.resolve()
    destination = state_dir / scratch.name
    policy = state_dir / ".gitignore"
    if destination.is_symlink():
        raise ValueError(
            f"baseline scratch directory {destination} is a symlink; "
            f"expected an unused directory path (policy {policy})"
        )
    owner = GoalCheckout(destination.parent).containing_repository()
    if owner is None:
        # Git never descends a symlink, so no repository can list a scratch that lives in the
        # target of a symlinked Main Booley Directory entry; that entry is the owner.
        entry = main_booley_dir_entry(root)
        if entry is not None and entry.is_symlink() and entry.resolve() == state_dir:
            return
        raise ValueError("baseline scratch has no repository owner")
    try:
        ignored = ignored_paths(
            owner[0].resolve(), [destination], directories=frozenset({destination})
        )
    except OSError as exc:
        raise ValueError(f"baseline scratch ignore query failed (policy {policy}): {exc}") from exc
    if destination not in ignored:
        raise ValueError(
            f"baseline scratch directory {destination} is not classified as ignored; "
            f"expected /.baseline-wt-*/ in {policy}"
        )


QUEUE_WAIT_LIMIT_S = 24 * 3600


@contextmanager
def _one_job_per_flow(request: Mapping[str, Any], family: BaselineFamily) -> Iterator[None]:
    """Serialize a family's baseline Jobs; the queue wait never spends the Job's budget.

    The waiting Job's recorded ``timeout_s`` grows by the time waited, so the
    measurement budget and every reader's deadline guard stay in agreement.
    """
    from booley.mcp.sim_baseline_jobs import BASELINE_TIMEOUT_S
    from booley.runtime import job_records

    jobs = Path(request["jobs_root"])
    lock_path = baseline_root(Path(request["booley_dir"]), family) / ".runtime" / "flow.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def extend_budget() -> None:
        job = job_records.read_record(request["job_id"], jobs)
        if job is not None:
            job.timeout_s = BASELINE_TIMEOUT_S + math.ceil(time.monotonic() - started)
            job_records.write_record(job, jobs)

    def narrate() -> None:
        print(f"[flow] waiting for the running {family.name} baseline Job", flush=True)
        extend_budget()

    with lock_path.open("a+b") as handle:
        wait_for_file_lock(handle, timeout_s=QUEUE_WAIT_LIMIT_S, on_wait=narrate)
        try:
            extend_budget()
            yield
        finally:
            release_file_lock(handle)


def run_baseline(
    path: Path, request: dict[str, Any], *, execute: Execute | None = None
) -> list[dict[str, Any]]:
    """Run one baseline Job, at most one per Flow family at a time."""
    with _one_job_per_flow(request, family_for_schema(request["schema"])):
        return _run_baseline_locked(path, request, execute)


def _run_baseline_locked(
    path: Path, request: dict[str, Any], execute: Execute | None
) -> list[dict[str, Any]]:
    """Resolve the base's own declarations, reuse contents evidence, then run missing work."""
    record = GoalRecord.from_json(request["record"])
    family = family_for_schema(request["schema"])
    root = Path(request["source_repository"])
    _ignored_destination(root, record.base_sha)
    paired = (
        PairedProjectBaseline.absent()
        if record.paired_project_base_sha is None
        else PairedProjectBaseline.entry_pinned(record.paired_project_base_sha)
    )
    with baseline_worktree(root, record.base_sha, paired_project=paired) as checkout:
        restore_entry_inputs(checkout, request)
        request["owned_checkout"] = str(checkout)
        request["scratch_parent"] = str(checkout.parent)
        request["owned_paired_checkout"] = (
            str(resolve_checkout_booley_dir(checkout)) if record.paired_project_base_sha else None
        )
        write_document(path, request)
        units = tuple(family.parse_unit(row) for row in request["identity"]["runs"])
        target_identity, results, applicable = EXECUTORS[family.name].classify(
            checkout, request, units
        )
        declaration = DEFAULT_RESOLVERS.surface_fingerprint(checkout, None)
        empty = {"digest": EMPTY_CATEGORY_DIGEST, "files": []}
        declaration_proof = prove_base_contents(
            checkout,
            request,
            dict.fromkeys(goal_required_categories(family.freshness_key), empty)
            | {"target_surface": declaration},
        )
        declaration_proof["target_identity"] = target_identity
        for result in results:
            result["proof"] = declaration_proof
        if not applicable:
            return results
        target = request["identity"]["target"]
        fingerprint = DEFAULT_RESOLVERS.fingerprint(checkout, target=target)
        proof = prove_base_contents(checkout, request, fingerprint)
        proof["target_identity"] = target_identity
        return results + _observe_missing(
            checkout, path, request, applicable, proof, execute or execute_child
        )


def classify_sim_runs(
    checkout: Path, request: Mapping[str, Any], runs: tuple[TestRun, ...]
) -> tuple[str | None, list[dict[str, Any]], tuple[TestRun, ...]]:
    """The SIM adapter's classification of the base's own Test Run declarations."""
    plans = TargetPlans(checkout)
    target = request["identity"]["target"]
    plan = plans.plan(target)
    results, applicable = _classify_runs(plan, runs)
    return (plans.identity(target) if plan is not None else None), results, applicable


def _classify_runs(
    plan: TargetTestPlan | None, runs: Sequence[TestRun]
) -> tuple[list[dict[str, Any]], tuple[TestRun, ...]]:
    results, applicable = [], []
    for run in runs:
        kind, reason = baseline_kind(plan, run)
        if kind == "runnable":
            applicable.append(run)
        else:
            results.append({**test_run_document(run), "grade": kind, "reason": reason})
    return results, tuple(applicable)


def _cache_lock_path(project: Path, proof: Mapping[str, Any], family: BaselineFamily) -> Path:
    key = {
        "target": proof["target_identity"],
        "contents": proof["contents"],
        "protected_inputs": proof["protected_digest"],
    }
    return baseline_root(project, family) / "locks" / (content_digest(key) + ".lock")


def _observe_missing(checkout, path, request, runs, proof, execute) -> list[dict[str, Any]]:
    project, target, stamp = (
        Path(request["booley_dir"]),
        request["identity"]["target"],
        proof["contents"],
    )
    family = family_for_schema(request["schema"])
    cache_target = proof["target_identity"]
    lock_path = _cache_lock_path(project, proof, family)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        wait_for_file_lock(handle, timeout_s=_remaining_seconds(path.parent))
        try:
            lookups = [
                (
                    run,
                    cached_result(
                        project,
                        family.result_key(cache_target, run, stamp, proof["protected_digest"]),
                        family,
                    ),
                )
                for run in runs
            ]
            missing = tuple(run for run, hit in lookups if hit is None)
            observed = (
                execute(checkout, target, missing, path.parent)
                if missing
                else {"rows": [], "stamps": [stamp]}
            )
            after = DEFAULT_RESOLVERS.fingerprint(checkout, target=target)
            if (
                contents_stamp(after, family.freshness_key) != stamp
                or any(value != stamp for value in observed.get("stamps", []))
                or (
                    not observed.get("stamps")
                    and any(row.get("grade") in {"pass", "fail"} for row in observed["rows"])
                )
            ):
                raise ValueError(
                    "baseline producer contents differ from the proven base fingerprint"
                )
            prove_base_contents(checkout, request, after)
            return [
                _publish_run(project, family, cache_target, run, proof, hit, observed)
                for run, hit in lookups
            ]
        finally:
            release_file_lock(handle)


def _publish_run(project, family, target, run, proof, cached, observed) -> dict[str, Any]:
    document = family.unit_document(run)
    if cached is not None:
        return {**document, "grade": cached["grade"], "proof": cached["proof"]} | _measurement(
            [cached]
        )
    matching = [
        row
        for row in observed["rows"]
        if row.get("name") == document["name"]
        and row.get("fingerprint") == document["fingerprint"]
    ]
    grades = {row.get("grade") for row in matching}
    grade = "fail" if "fail" in grades else "pass" if grades == {"pass"} else "unavailable"
    result = {**document, "grade": grade, "proof": proof} | _measurement(matching)
    if grade == "unavailable":
        reason = (
            "; ".join(str(row["reason"]) for row in matching if row.get("reason"))
            or observed.get("reason")
            or "baseline Test Run produced no graded result"
        )
        result["reason"] = " ".join(str(reason).split())[:300]
    else:
        key = family.result_key(target, run, proof["contents"], proof["protected_digest"])
        write_document(
            cache_path(project, key, family),
            {"schema": family.schema, "key": key, "grade": grade, "proof": proof}
            | _measurement(matching),
        )
    return result


def _measurement(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The family's measured evidence (SIM rows carry none), kept with the result."""
    return next(({"measurement": row["measurement"]} for row in rows if "measurement" in row), {})


@contextmanager
def _background_slot(owner: Mapping[str, Any], output: Path) -> Iterator[job_slots.SlotToken]:
    """Hold one background-priority slot of the Flow's Job Class while measuring.

    The supervisor, the process recorded on the Job, owns the claim so Job
    status shows the wait. The wait spends the Job's own remaining budget:
    a starved baseline ends unavailable rather than as a live Job that
    readers report failed.
    """
    from booley.config.jobs import parse_caps
    from booley.mcp.sim_baseline_jobs import BASELINE_TIMEOUT_S
    from booley.runtime.job_records import _proc_cmdline
    from booley.runtime.shared_infra import load_job_budget_config

    booley_dir = Path(owner["booley_dir"])
    job_class = EXECUTORS[family_for_schema(owner["schema"]).name].job_class()
    store = job_slots.SlotStore(
        job_slots.slots_dir(booley_dir), parse_caps(load_job_budget_config(booley_dir=booley_dir))
    )
    deadline = time.monotonic() + _remaining_seconds(output)
    pid = os.getpid()

    def narrate(position: int | None) -> None:
        line = f"baseline waiting for {job_class} slot (position {(position or 0) + 1})"
        print(
            f"[slot] {line}; held by {store.describe_holders(job_class)}",
            file=sys.stderr,
            flush=True,
        )

    try:
        token = store.acquire(
            job_class,
            pid=pid,
            argv=_proc_cmdline(pid) or [],
            role=job_slots.ROLE_BACKGROUND,
            # Twice the Job budget keeps the reaper's holder deadline a strict
            # upper bound of any legitimate measurement.
            timeout_s=2.0 * BASELINE_TIMEOUT_S,
            on_queued=narrate,
            should_abort=lambda: time.monotonic() >= deadline,
        )
    except job_slots.ClaimAbortedError as exc:
        raise TimeoutError(
            f"baseline Job exceeded its total execution budget while waiting for a {job_class} slot"
        ) from exc
    try:
        yield token
    finally:
        store.release(token)


def execute_child(
    checkout: Path, target: str, runs: tuple[TestRun, ...], output: Path
) -> dict[str, Any]:
    """A watchdog survives MCP exit and cleans the whole simulator process group.

    The child runs under the supervisor's background slot claim, so its own
    Flow admission claims nothing (``INHERITED_SLOT_ENV``).
    """
    from booley.mcp.sim_baseline_jobs import baseline_environment

    request_path = output / "request.json"
    owner = read_document(request_path, None)
    family = family_for_schema(owner["schema"])
    document = output / f"{family.child_stem}-request.json"
    write_document(
        document,
        {
            "schema": family.schema,
            "checkout": str(checkout),
            "target": target,
            "identity": {"runs": [family.unit_document(run) for run in runs]},
            "output": str(output),
        },
    )
    command = [sys.executable, "-m", "booley.mcp.sim_baseline_execution", str(document)]
    with _background_slot(owner, output) as token:
        environment = baseline_environment() | {
            job_slots.INHERITED_SLOT_ENV: f"{token.job_class}/{token.path.name}"
        }
        process = subprocess.Popen(command, env=environment, **new_group_kwargs())
        group = capture_process_group(process)
        from booley.runtime.pid import capture_process_identity

        identity = capture_process_identity(process.pid)
        write_document(
            request_path,
            owner | {"child_process": None if identity is None else identity.to_payload()},
        )
        try:
            process.wait(timeout=_remaining_seconds(output))
            return read_document(output / f"{family.child_stem}-result.json", family)
        finally:
            terminate_process_group(process, group)


def reap_owned_checkout(request: Mapping[str, Any], supervisor_pid: int) -> None:
    """Recover SIGKILL debris only when ownership, pins and process idleness agree."""
    from booley.core.process_identity import ProcessIdentity
    from booley.flows.baseline_worktree import _cleanup_baseline_worktree
    from booley.runtime.pid import ProcessState, observe_process
    from booley.runtime.pinned_history import raw_git

    value = request.get("owned_checkout")
    if value is None:
        return
    checkout, source = Path(value), Path(request["source_repository"])
    if not checkout.exists():
        return
    if (
        checkout.parent != Path(request["scratch_parent"])
        or not checkout.name.startswith(f".baseline-wt-{supervisor_pid}-")
        or checkout.is_symlink()
    ):
        raise ValueError("interrupted baseline scratch ownership differs")
    child = ProcessIdentity.from_payload(request.get("child_process"))
    if request.get("child_process") is not None and (
        child is None
        or observe_process(child).state not in {ProcessState.DEAD, ProcessState.ZOMBIE}
    ):
        return  # no cleanup while a child may still consume the checkout
    if GoalCheckout(checkout).head_sha() != request["identity"]["base_sha"]:
        raise ValueError("interrupted baseline checkout pin differs")
    actual = Path(
        os.fsdecode(
            raw_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
    )
    expected = Path(
        os.fsdecode(
            raw_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
    )
    if actual != expected:
        raise ValueError("interrupted baseline repository differs")
    paired = request.get("owned_paired_checkout")
    if paired is not None:
        paired = Path(paired)
        if (
            not paired.is_relative_to(checkout)
            or GoalCheckout(paired).head_sha() != request["identity"]["paired_base_sha"]
        ):
            raise ValueError("interrupted paired baseline ownership differs")
    _cleanup_baseline_worktree(source, checkout, paired)


def _remaining_seconds(output: Path) -> float:
    from booley.runtime import job_records

    request = read_document(output / "request.json", None)
    job = job_records.read_record(request["job_id"], Path(request["jobs_root"]))
    started = None if job is None else job_records.parse_stamp(job.started_at)
    if started is None:
        raise ValueError("baseline Job has no valid watchdog start time")
    remaining = started + job.timeout_s - time.time() - 180
    if remaining <= 0:
        raise TimeoutError("baseline Job exceeded its total execution budget")
    return min(3600, remaining)


class BaselineExecution(StandaloneFlowExecution):
    """Capture producer samples and authenticated campaigns, with no state persistence."""

    def __init__(self, checkout: Path, target: str) -> None:
        self.checkout, self.target = checkout, target
        self.campaigns: list[StartedCampaign] = []
        self.stamps: list[str] = []

    def register_campaign(self, campaign: StartedCampaign) -> None:
        self.campaigns.append(campaign)
        self.stamps.append(
            contents_stamp(DEFAULT_RESOLVERS.fingerprint(self.checkout, target=self.target))
        )

    def criterion_source_fingerprint(
        self, key: str, root: Path, target: str | None
    ) -> dict[str, Any]:
        value = DEFAULT_RESOLVERS.fingerprint(root, target=self.target)
        self.stamps.append(contents_stamp(value))
        return value


def simulate(request: Mapping[str, Any]) -> dict[str, Any]:
    """The actual Simulation Flow seam: standalone execution plus authenticated results."""
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest
    from booley.flows.sim.unpublished_results import read_unpublished_results

    checkout, target, output = (
        Path(request["checkout"]),
        request["target"],
        Path(request["output"]),
    )
    runs = request_runs(request)
    adapter = BaselineExecution(checkout, target)
    unnamed = len(runs) == 1 and runs[0].test is None
    result = SimulateFlow().execute(
        SimRequest(
            target=target,
            work_dir=checkout,
            test=None if unnamed else tuple(run.name for run in runs),
            seed=runs[0].seed if unnamed else None,
            report_dir=output / "reports",
        ),
        adapter=adapter,
    )
    rows: list[dict[str, Any]] = []
    for campaign in adapter.campaigns:
        durable = read_unpublished_results(campaign.manifest_path, campaign.manifest_sha256)
        rows.extend(dict(row) for row in durable.rows)
        _exclude_uncompleted(rows)
    return {
        "schema": SCHEMA,
        "rows": rows,
        "stamps": adapter.stamps,
        "reason": result.outcome.report_text,
    }


def _exclude_uncompleted(rows: list[dict[str, Any]]) -> None:
    """Authenticated Campaign rows retain whether a failure was functional.

    Shared Simulation grades an aborted design run as fail. Baseline reporting
    instead needs a completed functional failure, so it reads the classification
    the public authenticated reader retained from the immutable observation.
    """
    for row in rows:
        if row.get("grade") == "fail" and row.get("functional_failure") is not True:
            row["grade"] = "unavailable"
            row["reason"] = "baseline Test Run " + str(
                row.get("failure_execution", "did not complete")
            )


def _child_deadline(_signal: int, _frame: object) -> None:
    raise TimeoutError("baseline simulation exceeded its total Job budget")


def _child_watchdog(output: Path) -> threading.Timer:
    """Keep the child's total budget bounded even if its supervisor is killed."""
    timer = threading.Timer(
        _remaining_seconds(output), lambda: request_group_termination(ProcessGroup(os.getpid()))
    )
    timer.daemon = True
    timer.start()
    return timer


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    request = read_document(Path(args[0]), None)
    family = family_for_schema(request["schema"])
    output = Path(request["output"]) / f"{family.child_stem}-result.json"
    previous = signal.signal(signal.SIGTERM, _child_deadline)
    timer = _child_watchdog(Path(request["output"]))
    try:
        write_document(output, EXECUTORS[family.name].measure(request))
    except (OSError, ValueError, RuntimeError) as exc:
        write_document(
            output, {"schema": family.schema, "rows": [], "stamps": [], "reason": str(exc)}
        )
        return 2
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous)
    return 0


def _measure_sim(request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve ``simulate`` at call time so the Simulation Flow seam stays patchable."""
    return simulate(request)


def _sim_job_class() -> str:
    """The Simulation Flow's own Job Class declaration."""
    from booley.flows.sim.flow import SimulateFlow

    return SimulateFlow()._resolve_job_class()


#: Executor half of each registered family, keyed by ``BaselineFamily.name``.
EXECUTORS: dict[str, BaselineExecutor] = {
    "sim": BaselineExecutor(
        classify=classify_sim_runs, measure=_measure_sim, job_class=_sim_job_class
    ),
    "synth": BaselineExecutor(
        classify=classify_synth, measure=measure_synth, job_class=synth_job_class
    ),
}

if __name__ == "__main__":
    raise SystemExit(main())
