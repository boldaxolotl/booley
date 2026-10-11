"""Real Git cleanliness at Project runtime publication boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.project_runtime_git_support import (
    NEW_RUNTIME_PATHS,
    assert_clean,
    git,
    initialize_repository,
)

from booley.feedback.findings import Finding, append, rewrite
from booley.harness.init_cmd import InitContext, _backfill_project_gitignore
from booley.runtime import project_dir

# Audit policy owners, including interrupted publications. Concrete writer calls
# below complement these path fixtures; these are deliberately Git-policy tests.
RUNTIME_PATHS = (
    *NEW_RUNTIME_PATHS,
    "reviewer-evidence/.audit.json.tmp",
    "setup-evidence/attachments/.log.txt.json.tmp",
    "tmp/setup/run/manifest.json",
    "tmp/doctor/report.json",
    "tmp/fusesoc-isolated-cores/demo.core",
    "flow-reports/sim/1/report.json",
    "flow-reports/fpga/.report.json.tmp",
    ".runtime/build/run.log",
    ".runtime/traces/results.json",
    ".runtime/compiler-cache/cache-tag",
    ".runtime/sessions/log.json",
    ".runtime/mutation_tester/mutant.sv",
    ".runtime/executions/id/.record.json.tmp",
    ".runtime/campaign-child-executions/entries/id.json",
    ".managed/project-git-hooks.pyz",
    ".managed/.project-git-hooks.pyz.interrupted.tmp",
    "runtime/doctor_stamp.json",
    "runtime/sessions/thread.json",
    "runtime/jobs/slots/heavy/token.json",
    ".interactive_logs/session.log",
    "goals/g1/record.json",
    "goals/g1/.record.json.tmp",
    "goals/locks/work.lock",
    "tickets/board/ticket.md",
    "tickets/state/state.json",
    "tickets/waiver-candidates/candidate.json",
    "tickets/logs/run.log",
    "tickets/locks/ticket.lock",
    "worktrees/run/rtl/top.sv",
    "logs/diagnostic.log",
    ".baseline-wt-old/rtl/top.sv",
    "hooks/__pycache__/hook.pyc",
    "SETUP-REPORT.md",
    "FEEDBACK-REPORT.md",
)
AUTHORED_PATHS = (
    "booley.toml",
    "tests.toml",
    "criteria.toml",
    "AGENTS.md",
    "FUSESOC_IGNORE",
    "SETUP-PLAN.md",
    "doctor-waivers.toml",
    "cores/top.core",
    "selftest/sim/fixture.sv",
    "goalsets/default.md",
    "hooks/check.py",
    "goals/history/g1.md",
    "tickets/history/ticket.md",
    "cores/logs/result.log",
    "custom-output/report.json",
    *(f"{prefix}/{path}" for prefix in ("cores", "selftest") for path in NEW_RUNTIME_PATHS),
)


@pytest.fixture(params=["standalone", "nested"])
def versioned_project(tmp_path, monkeypatch, request):
    root = tmp_path / "design"
    data = root / ".booley_project"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text('[project]\nname = "fixture"\n')
    repo = data if request.param == "standalone" else root
    initialize_repository(repo, data, monkeypatch)
    project_dir.reset_cache()
    yield root, data, repo
    project_dir.reset_cache()


@pytest.mark.parametrize("path", RUNTIME_PATHS)
def test_runtime_policy_hides_only_local_outputs(versioned_project, path):
    _root, data, repo = versioned_project
    output = data / path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("local evidence\n")
    assert output.is_file()
    assert_clean(repo)


@pytest.mark.parametrize("path", AUTHORED_PATHS)
def test_authored_inputs_remain_trackable(versioned_project, path):
    _root, data, _repo = versioned_project
    output = data / path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("authored input\n")
    result = subprocess.run(
        ["git", "-C", str(data), "check-ignore", "--no-index", "--quiet", path],
        check=False,
        timeout=10,
    )
    assert result.returncode == 1, path


def test_findings_append_and_rewrite_are_local(versioned_project):
    _root, data, repo = versioned_project
    entry = append(Finding(title="local observation"), data)
    assert (data / "findings.jsonl").is_file()
    assert_clean(repo)
    entry.notes = "triaged"
    rewritten = rewrite([entry], data)
    assert "triaged" in rewritten.read_text()
    assert_clean(repo)


def test_interrupted_findings_rewrite_is_local(versioned_project, monkeypatch):
    _root, data, repo = versioned_project
    entry = append(Finding(title="original"), data)
    before = (data / "findings.jsonl").read_bytes()
    original = Path.replace

    def interrupted(source, target):
        if source == data / "findings.jsonl.tmp":
            raise OSError("interrupted replace")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted replace"):
        rewrite([entry], data)
    assert (data / "findings.jsonl.tmp").is_file()
    assert (data / "findings.jsonl").read_bytes() == before
    assert_clean(repo)


def test_backfill_preserves_already_tracked_findings(versioned_project):
    root, data, repo = versioned_project
    append(Finding(title="historical"), data)
    git(repo, "add", "-f", str(data / "findings.jsonl"))
    git(repo, "commit", "-qm", "existing tracked evidence")
    before = (data / "findings.jsonl").read_bytes()
    _backfill_project_gitignore(data, InitContext(project_root=root))
    assert (data / "findings.jsonl").read_bytes() == before
    assert git(repo, "ls-files", str(data / "findings.jsonl"))
    append(Finding(title="new observation"), data)
    assert " M " in git(repo, "status", "--short", "--untracked-files=all")


def _publish_bookkeeping(root, data, repo):
    """Doctor, MCP sessions/jobs, supervision: runtime/ and .runtime/."""
    from tests.mcp_tools.test_session_registry import facts

    from booley.harness.doctor_stamp import record_clean_run
    from booley.mcp.session_registry import SessionRegistry
    from booley.runtime.execution_records import atomic_write_json, execution_paths
    from booley.runtime.job_records import JobRecord, write_record

    stamp = record_clean_run(data, root, deep=False)
    assert stamp is not None and stamp.is_file()
    assert_clean(repo)
    SessionRegistry(data).upsert(facts(), "sim", "completed", now=100)
    sessions = tuple((data / "runtime/sessions").glob("*.json"))
    assert sessions
    assert_clean(repo)
    jobs = data / "runtime/jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    write_record(JobRecord("run", "sim", "2026-10-10T00:00:00Z", 10), jobs)
    assert (jobs / "run.json").is_file()
    assert_clean(repo)
    execution = execution_paths("a" * 32, project_dir=data).record
    atomic_write_json(execution, {"schema_version": 1, "state": "starting"})
    return (stamp, *sessions, jobs / "run.json", execution)


def _publish_goal_and_ticket(root, data, repo):
    """Goal records/locks and retained Ticket state: separate local owners."""
    from tests.goals.test_store import _enter

    from booley.criteria.state import DevelopmentState
    from booley.goals.paths import record_paths
    from booley.goals.store import GoalStore

    store = GoalStore(data)
    record = _enter(store, repo, "audit-20261010T000000Z")
    paths = record_paths(data, record.id)
    assert paths.record_file.is_file()
    assert_clean(repo)
    with store.record_lock(record.id):
        assert paths.lock_file.is_file()
        assert_clean(repo)
    ticket = data / "tickets/state/audit.json"
    state = DevelopmentState.load(ticket)
    state.init_criteria({"lint_clean": True})
    state.save()
    return (paths.record_file, paths.lock_file, ticket)


def _publish_logs_and_cache(root, data, repo):
    """Build/trace logs, mutation proposals, diagnostics, MCP transcripts."""
    from booley.dev_support.mutation_lock import LockMeta, lock_json_path, save_lock
    from booley.flows.run_log import write_run_log
    from booley.flows.sim.trace_session import TraceSession
    from booley.runtime.project_dir import runtime_dir

    runtime = runtime_dir(root)
    outputs = []
    for directory in (
        runtime / "build",
        data / "logs",
        data / "tickets/logs",
        data / ".interactive_logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        outputs.append(write_run_log(directory, "actual diagnostic publication\n"))
        assert outputs[-1].is_file()
        assert_clean(repo)
    save_lock(LockMeta(), runtime)
    outputs.append(lock_json_path(runtime))
    assert outputs[-1].is_file()
    assert_clean(repo)
    trace = TraceSession(runtime / "traces", backend="verilator", target="sim", test="smoke")
    trace.record_attempt("simulation", "started")
    outputs.append(trace.manifest_path)
    return tuple(outputs)


def _publish_cleanup_and_projection(root, data, repo):
    """Setup recovery and isolated FuseSoC projection: tmp/."""
    from booley.fusesoc.core_projection import reconcile_isolated_registry
    from booley.harness.setup.cleanup import prepare_run

    scratch = prepare_run(root, "audit")
    assert (scratch / "manifest.json").is_file()
    assert_clean(repo)
    projection = reconcile_isolated_registry(root)
    assert projection.written
    return (scratch / "manifest.json", *projection.written)


def test_audited_storage_families_keep_project_clean(versioned_project):
    root, data, repo = versioned_project
    # Authored core/config are committed before the runtime projection writer.
    (data / "booley.toml").write_text("[stealth]\nenabled = true\nignore_native_cores = true\n")
    core = data / "cores/demo.core"
    core.parent.mkdir()
    core.write_text("CAPI=2:\nname: booley::demo:0\nfilesets: {}\ntargets: {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "authored projection inputs")
    for publish in (
        _publish_bookkeeping,
        _publish_logs_and_cache,
        _publish_cleanup_and_projection,
    ):
        outputs = publish(root, data, repo)
        assert outputs, publish.__name__
        assert all(path.is_file() for path in outputs), outputs
        assert_clean(repo)
    outputs = _publish_goal_and_ticket(root, data, repo)
    assert all(path.is_file() for path in outputs), outputs
    assert_clean(repo)
    # Git worktrees are themselves a runtime storage family. The real Git
    # command publishes administrative state and a checkout under its owner.
    for linked in (data / "worktrees/audit", data / ".baseline-wt-audit"):
        git(repo, "worktree", "add", "-q", "--detach", str(linked))
        assert (linked / ".git").is_file()
        assert_clean(repo)


@pytest.mark.parametrize("family", ["lint", "sim", "synth", "fpga"])
@pytest.mark.parametrize("owner", ["flow-reports", "tmp/doctor/flow-reports"])
def test_actual_flow_report_publication_keeps_project_clean(versioned_project, family, owner):
    from booley.flows import endpoint_reporting
    from booley.flows.fpga.flow import FpgaImplFlow
    from booley.flows.lint.flow import LintFlow
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.runtime.endpoint_execution import EndpointOutcome

    root, data, repo = versioned_project
    flow = {
        "lint": LintFlow,
        "sim": SimulateFlow,
        "synth": AsicSynthesizeFlow,
        "fpga": FpgaImplFlow,
    }[family]()
    flow.context._args = flow.request_type(target="demo", work_dir=root, report_dir=data / owner)
    with flow.context.publication_resources:
        path = endpoint_reporting.write_report(flow.context, EndpointOutcome(exit_code=0))
    assert path is not None and path.is_file()
    assert (data / owner / f"{family}.json").is_file()
    assert_clean(repo)
    if family == "lint":
        flow._lint_invocation_dir = flow.reserve_invocation_dir()
        with flow.context.publication_resources:
            path = flow._write_lint_report(["demo"], [], 0.0)
        assert path is not None and path.is_file()
        assert_clean(repo)


@pytest.mark.parametrize("family", ["synth", "fpga"])
def test_implementation_envelope_storage_keeps_project_clean(versioned_project, family):
    from tests.flows.test_implementation_report import _context, _run

    from booley.flows.implementation_publication import ImplementationPublisher
    from booley.flows.implementation_report import MetricPolicy, build_implementation_report

    root, data, repo = versioned_project
    reports = data / "flow-reports"
    invocation = reports / family / "1"
    invocation.mkdir(parents=True)
    report = build_implementation_report(
        _context(flow=family), _run(), None, MetricPolicy(("area",))
    )
    published = ImplementationPublisher(root, reports, invocation).publish_report(
        report, {"passed": True}
    )
    assert published.stable_path.is_file()
    assert published.invocation_path.is_file()
    assert_clean(repo)


def test_compiler_cache_preparation_keeps_project_clean(versioned_project):
    from booley.flows.sim.compiler_cache import _prepare_directory, resolve_policy
    from booley.runtime.compiler_cache import IssuedCacheIdentity

    root, data, repo = versioned_project
    policy = resolve_policy(root, issued=IssuedCacheIdentity(None, False), owner=data)
    assert policy.root is not None
    _prepare_directory(policy.root)
    assert policy.root.is_dir()
    assert_clean(repo)


def test_campaign_publications_and_run_lock_keep_project_clean(versioned_project):
    from tests.flows.sim.test_campaign_manifest_codec import _manifest

    from booley.flows.sim.campaign.codec import (
        canonical_json_bytes,
        decode_simulation_campaign_manifest,
    )
    from booley.flows.sim.campaign.run_directory import (
        claimed_run_directory,
        expand_run_directory,
    )
    from booley.flows.sim.campaign.store import CampaignStore

    root, data, repo = versioned_project
    store = CampaignStore(data / ".runtime/campaigns/audit")
    store.publish_manifest(decode_simulation_campaign_manifest(canonical_json_bytes(_manifest())))
    assert store.manifest_path.is_file()
    assert_clean(repo)
    store.regenerate_summary()
    assert store.summary_path.is_file()
    assert_clean(repo)
    run = expand_run_directory(
        str(data / ".runtime/campaign-runs/{attempt}"),
        project_root=root,
        campaign_id="audit",
        target_key="sim",
        work_item_key="smoke",
        attempt_key="first",
    )
    identity = {"campaign_id": "audit", "work_item_id": "smoke", "attempt_id": "first"}
    with claimed_run_directory(run, identity=identity) as directory:
        assert (directory / ".booley-simulation-attempt.json").is_file()
        assert run.lock_path.is_file()
        assert_clean(repo)
    assert not run.path.exists()
    assert run.lock_path.is_file()
    assert_clean(repo)


def test_doctor_probe_and_upgrade_publications_keep_project_clean(versioned_project):
    from booley.harness import developer_probe, synth_probe, upgrade_review

    _root, data, repo = versioned_project
    for path in (
        developer_probe.record_measurement(data, 1024),
        synth_probe.record_measurement(data, "synth", 1.0),
    ):
        assert path.is_file()
        assert_clean(repo)
    upgrade_review.observe(data, current_version="0.3.0", now="2026-10-10T00:00:00Z")
    assert upgrade_review.state_path(data).is_file()
    assert_clean(repo)


def test_job_slot_publication_keeps_project_clean(versioned_project):
    from booley.runtime.job_slots import SlotStore

    _root, data, repo = versioned_project
    store = SlotStore(data / "runtime/jobs/slots", now=lambda: 100.0)
    token = store.submit("light", pid=1, argv=["fixture"])
    try:
        assert token.path.is_file()
        assert_clean(repo)
    finally:
        store.release(token)
    assert_clean(repo)


def test_ignored_evidence_is_omitted_from_specialist_visible_inputs(versioned_project):
    from booley.specialists.specialist_workspace import _visible_paths

    _root, data, repo = versioned_project
    for name in NEW_RUNTIME_PATHS:
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local evidence\n")
    authored = data / "SETUP-PLAN.md"
    authored.write_text("authored setup plan\n")
    visible = _visible_paths(repo)
    assert authored.relative_to(repo) in visible
    assert all((data / name).relative_to(repo) not in visible for name in NEW_RUNTIME_PATHS)


def test_specialist_session_and_mutation_round_storage_keep_project_clean(
    versioned_project, monkeypatch
):
    from booley.dev_support.mutation_lock import verification_rounds_dir
    from booley.specialists.mutation_tester import MutationTesterSpecialist, VerificationOutcome
    from booley.specialists.reviewer import ReviewerSpecialist

    root, data, repo = versioned_project
    monkeypatch.chdir(root)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    endpoint = ReviewerSpecialist()
    endpoint._last_session_id = "fixture-session"
    endpoint._persist_session_id("reviewer")
    assert (data / ".runtime/sessions/reviewer.session_id").is_file()
    assert_clean(repo)
    MutationTesterSpecialist()._write_round_log(1, VerificationOutcome(True, True, True))
    assert (verification_rounds_dir() / "round_1.log").is_file()
    assert_clean(repo)


def test_init_upgrade_requires_committing_repaired_ignore_file(versioned_project):
    root, data, repo = versioned_project
    ignore = data / ".gitignore"
    ignore.write_text("\n".join(p for p in ignore.read_text().splitlines() if p[0:1] != "/"))
    git(repo, "add", str(ignore))
    git(repo, "commit", "-qm", "old project policy")
    assert_clean(repo)
    _backfill_project_gitignore(data, InitContext(project_root=root))
    assert " M " in git(repo, "status", "--short", "--untracked-files=all")
    git(repo, "add", str(ignore))
    git(repo, "commit", "-qm", "reconciled project policy")
    assert_clean(repo)


def test_retained_ticket_storage_families_keep_project_clean(versioned_project):
    from tests.ticket_board.test_waiver_candidates import _rejection

    from booley.ticket_board.board_layout import (
        waiver_candidates_lock_path,
        waiver_candidates_path,
    )
    from booley.ticket_board.persistence import atomic_write_once
    from booley.ticket_board.waiver_candidates import record_rejections

    _root, data, repo = versioned_project
    board = data / "tickets/board/audit.md"
    board.parent.mkdir(parents=True)
    assert atomic_write_once(board, b"runtime ticket record\n", mode=0o644)
    assert board.is_file()
    assert_clean(repo)
    tickets = data / "tickets"
    assert record_rejections(tickets, "audit", [_rejection()]) == 1
    assert waiver_candidates_path(tickets, "audit").is_file()
    assert waiver_candidates_lock_path(tickets, "audit").is_file()
    assert_clean(repo)


def test_dashboard_task_runtime_ownership_keeps_project_clean(versioned_project):
    from booley.runtime.dashboard_tasks import reconcile

    _root, data, repo = versioned_project
    transaction = reconcile(repo, data)
    assert transaction.applied
    assert (data / "runtime/dashboard-task.json").is_file()
    assert (data / "runtime/dashboard-task.lock").is_file()
    assert_clean(repo)
