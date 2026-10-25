"""Relative SYNTH Goals get a background baseline synthesis Job (#2046).

Only the synthesis Flow seam is faked: the baseline checkout, Target
resolution, contents stamps, row mapping, the generic engine and the supervisor
all run for real. Nothing here sleeps; background work is driven in-process.

Each test first asks :func:`_synth_family` for the registered family, so on a
tree without the synthesis adapter every node fails by assertion, before it
touches any module the adapter introduces.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.flows.implementation_report import (
    ImplementationContext,
    ImplementationRun,
    MetricPolicy,
    build_implementation_report,
)
from booley.fusesoc import fusesoc_registry
from booley.goals import sim_baseline_store as store
from booley.goals.entry import RecipeFamily
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.paths import record_paths
from booley.mcp import goal_tools
from booley.mcp import sim_baseline_execution as execution
from booley.mcp import sim_baseline_jobs as jobs
from booley.runtime import job_records
from tests.goals.conftest import counting_draw, enter_goals, entry_environment, git

pytestmark = pytest.mark.timeout(120)

AREA = {"area_increase_at_most": "5%"}
RELATIVE_TOP = {"family": "SYNTH", "target": "top", "thresholds": AREA}
RELATIVE_BASE = {"family": "SYNTH", "target": "base", "thresholds": AREA}
ABSOLUTE_BASE = {"family": "SYNTH", "target": "base", "thresholds": {"cell_count_max": 5000}}
SIM_TOP = {"family": "SIM", "target": "top"}
WAIT_S = 30.0


@pytest.fixture
def synth(layout, monkeypatch):
    """A layout whose entry freezes a fake synthesis recipe per Target."""
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.setattr(
        fusesoc_registry, "resolve_target_handle", lambda handle, *, build_root: handle
    )
    recipes = (
        RecipeFamily(
            "synthesis_ok_",
            "Synthesis",
            lambda _r, selector: {"flow": "synth", "target": selector},
        ),
    )
    monkeypatch.setattr(goal_tools, "_recipe_families", lambda: recipes)
    return SimpleNamespace(**vars(layout), calls=[], fingerprints={}, outcome={})


def _synth_family() -> Any:
    family = next((item for item in store.BASELINE_FAMILIES if item.name == "synth"), None)
    assert family is not None, "synth baseline family is not registered"
    return family


BASE_CORE = (
    "CAPI=2:\nname: ::base:0\nfilesets:\n  rtl: {files: [rtl.v], file_type: verilogSource}\n"
    "targets:\n  base: {filesets: [rtl], toplevel: top}\n"
)


def _enter(scene, goals, **changes):
    """Enter Goal Mode through the production entry environment (scheduling included)."""
    (scene.worktree / "base.core").write_bytes(BASE_CORE.encode())
    (scene.worktree / "rtl.v").write_bytes(b"module top; endmodule\n")
    git(scene.worktree, "add", "base.core", "rtl.v")
    git(scene.worktree, "commit", "-q", "-m", "base core")
    env = replace(
        goal_tools._entry_environment(scene.control, None), draw_seeds=counting_draw(), **changes
    )
    scene.record = enter_goals(scene, goals, environment=env)
    return scene.record


def _frozen(scene, selector: str, record=None) -> str:
    state_file = record_paths(scene.control, (record or scene.record).id).state_file
    params = json.loads(state_file.read_text("utf-8"))["criteria"][f"synthesis_ok_{selector}"][
        "params"
    ]
    return params["_recipe_fingerprint"]


def _requests(scene, name: str = "synth") -> list[Path]:
    root = scene.control / "goals" / f"{name}-baselines" / "requests"
    return sorted(root.glob("*/request.json"))


def _request_for(scene, selector: str) -> tuple[Path, dict[str, Any]]:
    """The one synth request whose baseline Target is *selector*."""
    family = _synth_family()
    found = [
        (path, store.read_document(path, family))
        for path in _requests(scene)
        if store.read_document(path, family)["identity"]["target"] == selector
    ]
    assert len(found) == 1, f"synth baseline request for {selector}"
    return found[0]


def _envelope(fingerprint: str, *, termination="completed", infra=None, passed=True) -> dict:
    """The canonical implementation envelope the synthesis Flow publishes."""
    run = ImplementationRun(
        passed=passed,
        tool_returncode=0,
        metrics={"area": 12.5, "cell_count": 40},
        timed_out=termination == "timeout",
        infra_error=infra,
        termination=termination,
        recipe_fingerprint=fingerprint,
        recipe_snapshot={"flow": "synth"},
    )
    context = ImplementationContext(flow="synth", target="top")
    return build_implementation_report(context, run, None, MetricPolicy(())).canonical


def _seam(scene, monkeypatch) -> Any:
    """Fake only the synthesis Flow run; ``scene.outcome[selector]`` overrides its envelope."""
    from booley.mcp import synth_baseline_execution as module

    def synthesize(_checkout, selector, _report_dir, _timeout_ms):
        scene.calls.append(selector)
        fingerprint = scene.fingerprints.get(selector) or _frozen(scene, selector)
        return _envelope(fingerprint, **scene.outcome.get(selector, {}))

    monkeypatch.setattr(module, "_synthesize", synthesize)
    return module


def _execute_in_process(module):
    """The child's measurement without a child process."""

    def execute(checkout, target, units, output):
        request = {
            "schema": store.SYNTH_BASELINE.schema,
            "checkout": str(checkout),
            "target": target,
            "identity": {"runs": [dict(unit) for unit in units]},
            "output": str(output),
        }
        return module.measure_synth(request)

    return execute


def _run(scene, module, path, request) -> list[dict[str, Any]]:
    """One Job's worth of engine work, published like the supervisor would."""
    rows = execution.run_baseline(path, request, execute=_execute_in_process(module))
    store.write_document(path, request | {"status": "complete", "results": rows})
    return rows


def _supervise(monkeypatch, path, *, child_wait=None) -> int:
    """Run the supervisor and its child in this process; return the Job's exit code."""

    def popen(command, **_kwargs):
        wait = child_wait or (lambda **_kw: execution.main([command[-1]]))
        return SimpleNamespace(pid=os.getpid(), wait=wait)

    # Replace the module reference, not ``subprocess.Popen``: Git runs through it too.
    monkeypatch.setattr(execution, "subprocess", SimpleNamespace(Popen=popen))
    monkeypatch.setattr(execution, "terminate_process_group", lambda *_args: None)
    monkeypatch.setattr(
        execution, "_child_watchdog", lambda _output: SimpleNamespace(cancel=lambda: None)
    )
    monkeypatch.setattr(jobs.signal, "signal", lambda *_args: None)
    return jobs.main([str(path)])


def _job(request: dict[str, Any]) -> Any:
    job = job_records.read_record(request["job_id"], Path(request["jobs_root"]))
    assert job is not None
    return job


def _evidence(scene) -> list[Path]:
    return sorted((scene.control / "goals" / "synth-baselines" / "evidence").glob("*.json"))


# ---------------------------------------------------------------------------
# AC1: entry and Goal Change schedule a Job for relative SYNTH Goals only
# ---------------------------------------------------------------------------


def test_entry_schedules_baseline_jobs_only_for_relative_synth_goals(synth):
    record = _enter(synth, [RELATIVE_TOP, ABSOLUTE_BASE, SIM_TOP])
    requests = _requests(synth)
    assert len(requests) == 1, "synth baseline request"
    identity = json.loads(requests[0].read_text("utf-8"))["identity"]
    assert identity["target"] == "top"
    assert identity["base_sha"] == record.base_sha
    assert identity["runs"] == [{"name": "top", "fingerprint": _frozen(synth, "top")}]
    assert len(_requests(synth, "sim")) == 1


def test_approved_goal_change_adding_relative_synth_goal_schedules_baseline(synth):
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.change_service import create_proposal

    _synth_family()
    record = _enter(synth, [{"family": "LINT", "target": "top"}])
    assert _requests(synth) == [], "no synth baseline request before the change"
    (synth.worktree / "rtl.v").write_bytes(b"module changed; endmodule\n")
    env = ChangeEnvironment(
        replace(entry_environment(synth), recipe_families=goal_tools._recipe_families())
    )
    proposal = create_proposal(
        env,
        record.id,
        {"kind": "add", "after": RELATIVE_TOP, "rationale": "area must not grow"},
        session_key="test",
    )
    result = asyncio.run(
        goal_tools.dispatch_goal_tool(
            goal_tools.GOAL_PROPOSE_CHANGE,
            {
                "work_dir": str(synth.worktree),
                "operation": "approve",
                "proposal_id": proposal.proposal.id,
                "reason": "area must not grow",
                "approval_quote": "Add the SYNTH Goal.",
            },
            booley_dir=synth.control,
        )
    )
    assert result.is_error is False
    requests = _requests(synth)
    assert len(requests) == 1, "synth baseline request"
    identity = json.loads(requests[0].read_text("utf-8"))["identity"]
    assert identity["base_sha"] == record.base_sha and identity["target"] == "top"


# ---------------------------------------------------------------------------
# AC2: the result key names Target identity, recipe, contents and Protected Inputs
# ---------------------------------------------------------------------------


def test_synth_result_key_names_target_recipe_contents_and_protected_inputs(synth, monkeypatch):
    _synth_family()
    record = _enter(synth, [RELATIVE_TOP])
    module = _seam(synth, monkeypatch)
    path, request = _request_for(synth, "top")
    rows = _run(synth, module, path, request)
    assert [row["grade"] for row in rows] == ["pass"]
    (evidence,) = _evidence(synth)
    document = store.read_document(evidence, store.SYNTH_BASELINE)
    stamp = execution.contents_stamp(
        DEFAULT_RESOLVERS.fingerprint(synth.worktree, target="top"), "synthesis_ok_baseline"
    )
    assert document["key"] == {
        "target": "::top:0#top",
        "name": "top",
        "fingerprint": _frozen(synth, "top"),
        "contents": stamp,
        "protected_inputs": record.protected_digest,
    }
    assert document["measurement"]["metrics"] == {"area": 12.5, "cell_count": 40}
    assert document["measurement"]["recipe"]["fingerprint"] == _frozen(synth, "top")
    assert rows[0]["measurement"] == document["measurement"]


@pytest.mark.parametrize("dimension", ["target", "recipe", "contents", "protected"])
def test_changed_key_dimension_misses(synth, monkeypatch, dimension):
    _synth_family()
    _enter(synth, [RELATIVE_TOP])
    module = _seam(synth, monkeypatch)
    path, request = _request_for(synth, "top")
    _run(synth, module, path, request)
    assert synth.calls == ["top"]
    _run(synth, module, path, request)
    assert synth.calls == ["top"], "an identical key is a hit"

    changed = json.loads(json.dumps(request))
    if dimension == "target":
        monkeypatch.setattr(
            module,
            "TargetCatalog",
            SimpleNamespace(
                build=lambda _root: SimpleNamespace(
                    select=lambda name: SimpleNamespace(selector=name, identity="::other:0#top")
                )
            ),
        )
    elif dimension == "recipe":
        changed["identity"]["runs"][0]["fingerprint"] = synth.fingerprints["top"] = "recipe-2"
    elif dimension == "contents":
        real = execution.contents_stamp
        monkeypatch.setattr(execution, "contents_stamp", lambda fp, key=None: real(fp, key) + "x")
    else:
        changed["record"]["protected_digest"] = "sha256:" + "f" * 64
    _run(synth, module, path, changed)
    assert synth.calls == ["top", "top"], f"a changed {dimension} must measure again"
    assert len(_evidence(synth)) == 2


# ---------------------------------------------------------------------------
# AC3: a second session reuses the pending or complete request
# ---------------------------------------------------------------------------


def test_second_session_reuses_pending_and_complete_request(synth, monkeypatch):
    _synth_family()
    record = _enter(synth, [RELATIVE_TOP])
    module = _seam(synth, monkeypatch)
    other = synth.control / "worktrees" / "second"
    git(synth.main, "worktree", "add", "-q", "--detach", str(other), record.base_sha)
    second = replace(record, id="second-20261006T130000Z", worktree_path=str(other))
    second_paths = record_paths(synth.control, second.id)
    second_paths.root.mkdir(parents=True, exist_ok=True)
    first_paths = record_paths(synth.control, record.id)
    inputs = store.entry_inputs(synth.control, record)
    store.write_document(second_paths.root / store.INPUTS_FILE, inputs)
    second_paths.state_file.write_bytes(first_paths.state_file.read_bytes())

    launched = []
    jobs.schedule_goal_baselines(
        synth.control, second, launch=lambda path, _request: launched.append(path)
    )
    assert launched == [] and len(_requests(synth)) == 1, "pending request is reused"

    path, request = _request_for(synth, "top")
    _run(synth, module, path, request)
    jobs.schedule_goal_baselines(
        synth.control, second, launch=lambda path, _request: launched.append(path)
    )
    assert launched == [] and len(_requests(synth)) == 1, "complete request is reused"
    from booley.goals.sim_transitions import baseline_observations

    goal = _synth_family().goals(second)[0]
    seen = baseline_observations(synth.control, second, goal, _synth_family())
    assert seen["status"] == "complete" and seen["results"][0]["grade"] == "pass"


# ---------------------------------------------------------------------------
# AC4: at most one baseline Job per Flow runs at a time
# ---------------------------------------------------------------------------


def test_one_baseline_job_per_flow_runs_at_a_time(synth, monkeypatch):
    family = _synth_family()
    _enter(synth, [RELATIVE_TOP])
    module = _seam(synth, monkeypatch)
    path, request = _request_for(synth, "top")
    lock_path = store.baseline_root(synth.control, family) / ".runtime" / "flow.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    waiting = threading.Event()
    real_wait = execution.wait_for_file_lock

    def observed_wait(handle, *, timeout_s, on_wait=None, **kwargs):
        def waited():
            if on_wait is not None:
                on_wait()
            waiting.set()

        real_wait(handle, timeout_s=timeout_s, on_wait=waited, **kwargs)

    monkeypatch.setattr(execution, "wait_for_file_lock", observed_wait)
    outcome: dict[str, Any] = {}

    def worker():
        try:
            outcome["rows"] = _run(synth, module, path, request)
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome["error"] = exc

    with lock_path.open("a+b") as holder:
        acquire_file_lock(holder)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert waiting.wait(WAIT_S), "the second Job never queued behind the running one"
        assert synth.calls == [], "no synthesis runs while another baseline Job holds the Flow"
        assert _job(request).timeout_s > jobs.BASELINE_TIMEOUT_S, "queue wait is off budget"
        release_file_lock(holder)
    thread.join(WAIT_S)
    assert not thread.is_alive() and "error" not in outcome, outcome.get("error")
    assert synth.calls == ["top"]
    assert outcome["rows"][0]["grade"] == "pass"


# ---------------------------------------------------------------------------
# AC5: unfinished synthesis stores no result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["interrupted", "timeout", "infrastructure"])
def test_unfinished_synthesis_stores_no_result(synth, monkeypatch, ending):
    _synth_family()
    _enter(synth, [RELATIVE_TOP])
    _seam(synth, monkeypatch)
    path, request = _request_for(synth, "top")
    child_wait = None

    def interrupted(**_kwargs):
        raise InterruptedError("baseline Job cancelled")

    outcomes = {
        "timeout": {"termination": "timeout", "infra": "synthesis execution timeout"},
        "infrastructure": {"termination": "infrastructure_error", "infra": "yosys did not start"},
    }
    if ending == "interrupted":
        child_wait = interrupted
    else:
        synth.outcome["top"] = outcomes[ending] | {"passed": False}
    assert _supervise(monkeypatch, path, child_wait=child_wait) == 2
    document = store.read_document(path, _synth_family())
    assert document["status"] == "unavailable" and document["reason"]
    assert all(row["grade"] == "unavailable" for row in document.get("results", []))
    assert _job(request).status == job_records.STATUS_FAILED
    assert _evidence(synth) == []


# ---------------------------------------------------------------------------
# AC6: status and the Job list show pending / complete / unavailable
# ---------------------------------------------------------------------------


def _baseline_lines(scene) -> list[str]:
    from booley.goals.format import render_status
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore

    view = build_status(GoalStore(scene.control), scene.record)
    return [line.strip() for line in render_status((view,)).splitlines() if ": baseline: " in line]


def _two_goal_scene(synth, monkeypatch):
    """Two relative SYNTH Goals; synthesis of ``base`` ends in a timeout."""
    _enter(synth, [RELATIVE_TOP, RELATIVE_BASE])
    module = _seam(synth, monkeypatch)
    synth.outcome["base"] = {
        "termination": "timeout",
        "infra": "synthesis execution timeout",
        "passed": False,
    }
    return module


def test_status_shows_synth_baseline_pending_complete_unavailable(synth, monkeypatch):
    _synth_family()
    _two_goal_scene(synth, monkeypatch)
    lines = _baseline_lines(synth)
    assert [line.rpartition(": baseline: ")[2] for line in lines] == ["pending", "pending"]

    for selector in ("top", "base"):
        _supervise(monkeypatch, _request_for(synth, selector)[0])
    lines = _baseline_lines(synth)
    states = {line.split(": baseline: ")[0].split(":")[1].split()[0]: line for line in lines}
    assert states["top"].endswith(": baseline: complete")
    assert ": baseline: unavailable (" in states["base"] and "timeout" in states["base"]


def test_job_list_shows_synth_baseline_job_state(synth, monkeypatch):
    from booley.harness.dashboard.jobs import TERMINAL_JOB_STATES, JobReader

    _synth_family()
    _two_goal_scene(synth, monkeypatch)
    reader = JobReader(synth.control)

    def states() -> dict[str, tuple[str, str]]:
        jobs_by_id = {view.record.run_id: view for view in reader.read((synth.record,)).jobs}
        return {
            selector: (jobs_by_id[request["job_id"]].state, jobs_by_id[request["job_id"]].handle)
            for selector in ("top", "base")
            for _path, request in [_request_for(synth, selector)]
        }

    before = states()
    assert all(state not in TERMINAL_JOB_STATES for state, _ in before.values())
    for selector in ("top", "base"):
        _supervise(monkeypatch, _request_for(synth, selector)[0])
    after = states()
    assert after["top"][0] == "completed" and after["base"][0] == "failed"
    assert all(handle.startswith("J-SYN") for _, handle in after.values())


def test_synth_row_maps_envelopes_to_results_or_unavailable():
    from booley.mcp.synth_baseline_execution import synth_row

    unit = {"name": "top", "fingerprint": "f1"}
    assert synth_row(unit, _envelope("f1"))["grade"] == "pass"
    assert synth_row(unit, _envelope("f1", passed=False))["grade"] == "fail"
    drifted = synth_row(unit, _envelope("f2"))
    assert drifted["grade"] == "unavailable" and "recipe" in drifted["reason"]
    killed = synth_row(unit, _envelope("f1", termination="oom", infra="synthesis execution oom"))
    assert killed["grade"] == "unavailable" and "oom" in killed["reason"]
    failed_tool = synth_row(unit, _envelope("f1", termination="eda_tool_failure"))
    assert failed_tool["grade"] == "unavailable"


def test_synthesize_runs_the_standalone_flow_and_reads_its_canonical_report(tmp_path, monkeypatch):
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.flows.synth.recipe import default_recipe_args, synthesis_recipe_args
    from booley.mcp.synth_baseline_execution import _synthesize

    seen = []

    def execute(_flow, request, *, adapter):
        seen.append((request, adapter))
        report = request.report_dir / "synth_top.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"implementation": _envelope("f1")}), encoding="utf-8")

    monkeypatch.setattr(AsicSynthesizeFlow, "execute", execute)
    envelope = _synthesize(tmp_path, "top", tmp_path / "reports", 90_000)
    ((request, adapter),) = seen
    assert (request.target, request.work_dir, request.timeout_ms) == ("top", tmp_path, 90_000)
    assert type(adapter).__name__ == "StandaloneFlowExecution"
    # The Flow derives its recipe from this bare request: it must equal the one frozen at entry.
    frozen = synthesis_recipe_args({}, default_recipe_args(), target="top")
    assert synthesis_recipe_args({}, request, target="top") == frozen
    assert envelope["recipe"]["fingerprint"] == "f1"
