"""Runtime identity and evidence remain usable after publication of an amendment."""

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board.acceptance_ledger import freeze_acceptance, record_changes
from booley.ticket_board.amendment import apply_amendment, preview_amendment
from booley.ticket_board.paths import runtime_file

from .test_amendment_publication import _optional_request
from .test_ticket_baseline import _blocked_ticket


def test_amendment_refreshes_existing_mounted_ticket(tmp_path: Path, monkeypatch) -> None:
    from booley.evidence.review_receipt import ReviewInvocation, build_review_contract_detail

    _root, ticket, tio = _blocked_ticket(tmp_path)
    snapshot = tio.logs_dir / "blocked-again" / "ticket.md"
    snapshot.write_bytes(ticket.read_bytes())
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(snapshot.parent))
    invocation = ReviewInvocation(_root, "rtl", "bugs", (), "strict")
    old_contract = build_review_contract_detail(invocation)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    old_identity = tio.load_basis("blocked-again").ticket_identity()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)

    apply_amendment(tio, "blocked-again", request, preview["digest"])

    current = tio.load_basis("blocked-again", runtime_ticket_path=snapshot).ticket_identity()
    assert current != old_identity
    assert snapshot.read_bytes() == ticket.read_bytes()
    assert (
        build_review_contract_detail(invocation)["ticket_digest"] != old_contract["ticket_digest"]
    )


def test_valid_foreign_plain_evidence_does_not_gate_current_observation(tmp_path: Path) -> None:
    state = DevelopmentState()
    state.slug = "amended"
    state.init_criteria({"sim_pass": True})
    old = {
        "generation": "a" * 32,
        "authored_sha256": "b" * 64,
        "baseline": {"outer": {"commit": "c" * 40}},
    }
    new = {**old, "generation": "d" * 32}
    record_changes(
        tmp_path,
        state,
        state.set_criterion("sim_pass", False),
        invocation_id="old",
        producer="sim",
        execution_id="old",
        ticket_identity=old,
    )
    record_changes(
        tmp_path,
        state,
        state.set_criterion("sim_pass", True),
        invocation_id="new",
        producer="sim",
        execution_id="new",
        ticket_identity=new,
    )

    frozen = freeze_acceptance(
        tmp_path,
        state,
        execution_id="new",
        ticket_identity=new,
        participant_heads={"outer": "c" * 40},
    )

    assert [ref["sequence"] for ref in frozen.evidence] == [2]
    assert len(list((tmp_path / "acceptance/evidence").glob("*/record.json"))) == 2


def _simulation_ticket(tmp_path: Path):
    import yaml

    from booley.ticket_board.frontmatter import parse_frontmatter

    from .test_ticket_baseline import _basis_project, _git

    root, data, tio = _basis_project(tmp_path)
    (root / "dut.sv").write_text("module dut; endmodule\n")
    (root / "dut.core").write_text(
        "CAPI=2:\nname: acme:lib:dut:1\nfilesets:\n  rtl:\n"
        "    files: [dut.sv]\n    file_type: systemVerilogSource\n"
        "targets:\n  sim:\n    flow: sim\n    default_tool: verilator\n"
        "    flow_options: {tool: verilator}\n    filesets: [rtl]\n    toplevel: dut\n"
    )
    (data / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n')
    (root / "EXTRA.md").write_text("extra\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "simulation inputs")
    fields = {
        "summary": "Resume amended simulation",
        "type": "verification",
        "branch": "main",
        "scope": ["README.md"],
        "on_success": ["review"],
        "CRITERIA_MANDATORY": {
            "SIM": {"sim": {"smoke": "pass"}},
            "CYCLE_COUNT": {"sim": {"smoke": {"cycle_count_max": 400}}},
        },
    }
    ticket = tio.create_ticket_document(
        "amended", "---\n" + yaml.safe_dump(fields) + "---\n\n## Description\n\nResume.\n"
    )
    assert ticket is not None
    assert tio.enqueue_ticket("amended")
    assert tio.init_ticket(ticket) is not None
    from booley.ticket_board.lifecycle import TicketState

    assert tio.move_and_update(
        "amended",
        TicketState.RUNNING,
        {"step": "implementation"},
        append_step="setup",
        transition=("running:init", "running:setup", "test", "step complete"),
    )
    document = tio.load_document("amended")
    from booley.ticket_board.criteria_projection import project_ticket_criteria

    projection = project_ticket_criteria(document.spec)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "amended", "booley_state.json"))
    state.slug = "amended"
    state.ticket_type = "verification"
    state.init_criteria(
        {**projection.required, "_report_submitted": True},
        criterion_params=projection.params,
        strict=True,
        flow_key_aliases=projection.aliases,
    )
    state.save()
    return root, ticket, tio, state, parse_frontmatter


def _publish_simulation(
    root, tio, state, identity, directory, current=425, invalidate_cycle=False
):
    from booley.flows.sim.acceptance import AcceptanceContext, SimulationAcceptanceCoordinator
    from booley.ticket_board.flow_execution import TicketBoardFlowExecution
    from tests.flows.sim.test_campaign_phase2 import _cycle_outcome

    outcome = _cycle_outcome(directory, current=current)
    import json
    from dataclasses import replace

    from booley.flows.sim.campaign.facts import AcceptanceFacts, encode_acceptance_facts

    facts = json.loads(encode_acceptance_facts(outcome.acceptance_facts))
    observation = facts["observations"][0]
    facts["consumed_results"] = [
        {
            "work_item_id": observation["work_item_id"],
            "role": "candidate",
            "revision": "abc",
            "target": facts["target"],
            "attempt_id": "550e8400-e29b-41d4-a716-446655440000",
            "result": facts["prerequisites"][0]["result"],
            "finished_at": "2026-09-21T10:00:00Z",
        }
    ]
    outcome = replace(outcome, acceptance_facts=AcceptanceFacts(facts))
    recorder = TicketBoardFlowExecution(log_dir=tio.logs_dir / "amended", ticket_identity=identity)
    from booley.criteria.categories import verification_fingerprint_categories
    from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
    from booley.flows.criterion_freshness import build_criterion_freshness
    from booley.ticket_board.ticket_baseline import ticket_baseline_from_machine, worktree_for_ref

    checkout = worktree_for_ref(
        root, ticket_baseline_from_machine(identity).participant("outer").ticket_ref
    )
    assert checkout is not None
    state.work_dir = str(checkout)
    state.save()

    def stamp(change, selector):
        detail = {
            **change.detail,
            SOURCE_FINGERPRINT_DETAIL_KEY: build_criterion_freshness(
                checkout,
                target=selector,
                categories=tuple(verification_fingerprint_categories(change.key)),
            ).to_detail(),
        }

        if invalidate_cycle and change.key.startswith("cycle_count_"):
            stamp = detail[SOURCE_FINGERPRINT_DETAIL_KEY]
            category = stamp["categories"][0]
            stamp["fingerprint"][category]["digest"] = "expired source fingerprint"
        return detail

    result = SimulationAcceptanceCoordinator().reconcile(
        outcome,
        AcceptanceContext(
            "amended",
            identity,
            identity["generation"],
            state,
            recorder,
            "simulation",
            detail_stamper=stamp,
        ),
    )
    assert result.committed
    return result


def _apply_simulation_amendment(tio, cycle, kind):
    request = {"actor": "Human", "reason": "Accept amended execution", "feedback": "Resume"}
    if kind.startswith("threshold"):
        request["criteria"] = [{"criterion": cycle, "thresholds": {"cycle_count_max": 450}}]
    elif kind == "scope_add":
        request["scope_add"] = ["EXTRA.md"]
        # Scope-only amendment still needs a satisfied Cycle Count for handoff.
        # Publish a second deterministic Simulator result below with an allowed result.
    else:
        request["criteria"] = [{"criterion": cycle, "make_optional": True}]
    preview = preview_amendment(tio, "amended", request)
    return apply_amendment(tio, "amended", request, preview["digest"])


def _finish_amended_execution(tio, state, ctx, kind, directory, monkeypatch):
    from booley.flows.request import FlowRequest
    from booley.mcp.submit_run_report import SubmitRunReportMcpTool
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.ticket_board.acceptance_ledger import read_acceptance
    from booley.ticket_board.flow_execution import TicketBoardFlowExecution
    from booley.ticket_board.operations import op_handoff
    from booley.ticket_board.paths import human_log_file
    from booley.ticket_board.ticket_baseline import worktree_for_ref

    snapshot = tio.logs_dir / "amended/ticket.md"
    basis = tio.load_basis("amended")
    identity = basis.ticket_identity()
    cycle = next(
        name for name, entry in state.criteria.items() if "cycle_count_max" in entry.params
    )
    checkout = worktree_for_ref(ctx.project_root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    for key, value in {
        "BOOLEY_TICKET_FILE": snapshot,
        "BOOLEY_SLUG": "amended",
        "BOOLEY_LOGS_DIR": snapshot.parent,
        "BOOLEY_EXECUTION_ID": ctx.execution_id,
        "BOOLEY_STATE_FILE": state._file_path,
        "BOOLEY_TICKET_TYPE": "verification",
    }.items():
        monkeypatch.setenv(key, str(value))
    validation = TicketBoardFlowExecution().validate_and_resolve(
        FlowRequest(target="sim", work_dir=checkout)
    )
    assert not isinstance(validation, EndpointOutcome), validation
    state = DevelopmentState.load(state._file_path)
    _publish_simulation(
        ctx.project_root,
        tio,
        state,
        identity,
        directory,
        current=375 if kind == "scope_add" else 425,
    )
    state = DevelopmentState.load(state._file_path)
    if kind.startswith("threshold"):
        assert state.criteria[cycle].params["cycle_count_max"] == 450
        assert state.criteria[cycle].met
    elif kind == "make_optional":
        assert state.criteria[cycle].mandatory is False
    # Fresh final report uses the actual endpoint and all its finalization gates.
    code = SubmitRunReportMcpTool().main(
        [
            "--work-dir",
            str(checkout),
            "--summary",
            "Verified amended execution",
            "--coverage-added",
            "Simulation rerun",
            "--uncertainties",
            "None",
            "--file-justifications",
            "{}",
            "--optional-criteria-justification",
            "Human accepted the optional cycle limit",
        ]
    )
    assert code == 0
    human_log_file(tio.logs_dir, "amended", "run.log").write_text("Completed\n")
    assert op_handoff(tio, "amended", expected_execution_id=ctx.execution_id)
    assert tio.read_progress("amended")["step"] == "summary"
    frozen = read_acceptance(snapshot.parent).snapshot
    assert frozen is not None and frozen.ticket_identity == identity
    return frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["threshold", "scope_add", "make_optional", "threshold_reuse"])
@pytest.mark.parametrize("drop_snapshot", [False, True])
async def test_amended_ticket_resumes_simulation_and_reaches_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    drop_snapshot: bool,
) -> None:
    from booley.harness.setup.intake import run
    from booley.ticket_board.frontmatter import parse_frontmatter
    from booley.ticket_board.operations import op_block

    root, _ticket, tio, state, _ = _simulation_ticket(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    old = tio.load_basis("amended").ticket_identity()
    result = _publish_simulation(
        root, tio, state, old, tmp_path, invalidate_cycle=kind == "threshold"
    )
    state = DevelopmentState.load(state._file_path)
    cycle = next(
        name for name, entry in state.criteria.items() if "cycle_count_max" in entry.params
    )
    assert not state.criteria[cycle].met
    assert op_block(tio, "amended", "implementation", "Human amendment needed")
    applied = _apply_simulation_amendment(tio, cycle, kind)
    snapshot = tio.logs_dir / "amended/ticket.md"
    if drop_snapshot:
        snapshot.unlink()
    ctx = await run("amended", root)
    assert ctx.workspace_intent == "resume"
    assert ctx.completed_steps == ["setup"]
    basis = tio.load_basis("amended", runtime_ticket_path=snapshot)
    identity = basis.ticket_identity()
    assert identity != old
    fields, _ = parse_frontmatter(snapshot.read_text())
    if kind == "scope_add":
        assert "EXTRA.md" in fields["scope"]
    state = DevelopmentState.load(state._file_path)
    if kind.startswith("threshold"):
        assert state.criteria[cycle].met is (kind == "threshold_reuse")
    frozen = _finish_amended_execution(tio, state, ctx, kind, tmp_path / "fresh", monkeypatch)
    assert (
        result.transaction_id
        not in DevelopmentState.load(state._file_path).acceptance_transactions
    )
    assert all(ref["sequence"] > len(result.changes) for ref in frozen.evidence)
    assert (
        snapshot.parent / "amendments" / f"{applied['operation_id']}.prior-state.json"
    ).exists()


def _interrupt_amendment_publication(patch, boundary, ticket, snapshot):
    from booley.ticket_board import amendment

    replace_bytes = amendment.atomic_replace_bytes
    save_state = DevelopmentState.save
    write_journal = amendment._write_journal
    interrupted = False

    def maybe_interrupt(path, content, **kwargs):
        nonlocal interrupted
        replace_bytes(path, content, **kwargs)
        selected = ticket if boundary == "board" else snapshot
        if not interrupted and boundary in {"board", "snapshot"} and path == selected:
            interrupted = True
            raise OSError("publication interrupted")

    def state_interrupt(self):
        nonlocal interrupted
        save_state(self)
        if boundary == "state" and not interrupted:
            interrupted = True
            raise OSError("publication interrupted")

    def journal_interrupt(root, journal):
        nonlocal interrupted
        write_journal(root, journal)
        if boundary == "board_phase" and journal["phase"] == "board" and not interrupted:
            interrupted = True
            raise OSError("publication interrupted")

    patch.setattr(amendment, "atomic_replace_bytes", maybe_interrupt)
    patch.setattr(DevelopmentState, "save", state_interrupt)
    patch.setattr(amendment, "_write_journal", journal_interrupt)


@pytest.mark.parametrize("boundary", ["board", "snapshot", "state", "board_phase"])
def test_interrupted_amendment_reconciles_runtime_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import json

    _root, ticket, tio = _blocked_ticket(tmp_path)
    snapshot = tio.logs_dir / "blocked-again/ticket.md"
    snapshot.write_bytes(ticket.read_bytes())
    original_snapshot = snapshot.read_bytes()
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    original_state = state._file_path.read_bytes()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    with monkeypatch.context() as patch:
        _interrupt_amendment_publication(patch, boundary, ticket, snapshot)
        with pytest.raises(OSError, match="publication interrupted"):
            apply_amendment(tio, "blocked-again", request, preview["digest"])
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert result["status"] == "queued"
    tio.load_basis("blocked-again", runtime_ticket_path=snapshot)
    history = snapshot.parent / "amendments"
    assert (
        history / f"{result['operation_id']}.prior-ticket.md"
    ).read_bytes() == original_snapshot
    assert (history / f"{result['operation_id']}.prior-state.json").read_bytes() == original_state
    row = json.loads((history / f"{result['operation_id']}.json").read_text())
    assert row["old_ticket_identity"] != row["new_ticket_identity"]


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_state", ["absent", "malformed", "stale", "matching"])
async def test_intake_repairs_snapshot_after_owner_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_state: str,
) -> None:
    from booley.harness.setup.intake import run
    from booley.runtime.project_dir import reset_cache
    from booley.ticket_board.operations import op_activate

    root, ticket, tio = _blocked_ticket(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    reset_cache()
    snapshot = tio.logs_dir / "blocked-again/ticket.md"
    original = ticket.read_bytes()
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    from booley.ticket_board.lifecycle import TicketState

    tio.move_and_update("blocked-again", TicketState.BLOCKED, {"steps_completed": ["setup"]})
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    if snapshot_state == "absent":
        snapshot.unlink()
    elif snapshot_state == "malformed":
        snapshot.write_text("invalid snapshot")
    elif snapshot_state == "stale":
        snapshot.write_bytes(original)
    before = snapshot.stat().st_mtime_ns if snapshot.exists() else None
    ctx = await run(str(ticket), root)
    assert ctx.slug == "blocked-again"
    tio.load_basis(ctx.slug, runtime_ticket_path=snapshot)
    if snapshot_state == "matching":
        assert snapshot.stat().st_mtime_ns == before
    assert op_activate(tio, ctx.slug, execution_id=ctx.execution_id)


def test_live_owner_is_not_erased_by_identity_reads_or_activation(tmp_path: Path) -> None:
    import subprocess
    import sys

    from booley.ticket_board.operations import op_activate
    from booley.ticket_board.paths import existing_runtime_file

    _root, _ticket, tio = _blocked_ticket(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert op_activate(tio, "blocked-again", owner_pid=process.pid)
        snapshot = tio.logs_dir / "blocked-again/ticket.md"
        snapshot.write_text("old snapshot awaiting owner repair")
        lock = existing_runtime_file(tio.logs_dir, "blocked-again", "ticket.lock")
        before = (snapshot.read_bytes(), lock.read_bytes(), tio.read_progress("blocked-again"))
        tio.load_basis("blocked-again")
        assert not op_activate(tio, "blocked-again")
        assert tio.init_ticket(tio.find_ticket("blocked-again")["file"]) is None
        assert (
            snapshot.read_bytes(),
            lock.read_bytes(),
            tio.read_progress("blocked-again"),
        ) == before
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("boundary", ["record", "state"])
def test_amendment_proof_transaction_recovers_without_duplicate_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import json

    from booley.ticket_board import acceptance_ledger
    from booley.ticket_board.acceptance_ledger import record_amendment_observations

    state = DevelopmentState()
    state.slug = "retained"
    state.init_criteria({"first": True, "second": True})
    changes = state.set_criterion("first", True) + state.set_criterion("second", True)
    identity = {"generation": "a" * 32}
    write_once = acceptance_ledger._write_once
    writes = 0

    def interrupt(path, content):
        nonlocal writes
        if path.name == "record.json":
            writes += 1
            if writes == 2:
                raise OSError("record interrupted")
        write_once(path, content)

    if boundary == "record":
        with monkeypatch.context() as patch:
            patch.setattr(acceptance_ledger, "_write_once", interrupt)
            with pytest.raises(OSError, match="record interrupted"):
                record_amendment_observations(
                    tmp_path, state, changes, operation_id="operation", ticket_identity=identity
                )
    else:
        record_amendment_observations(
            tmp_path, state, changes, operation_id="operation", ticket_identity=identity
        )
        state.acceptance_transactions = []  # crash before saving the selected transaction
    record_amendment_observations(
        tmp_path, state, changes, operation_id="operation", ticket_identity=identity
    )
    record_amendment_observations(
        tmp_path, state, changes, operation_id="operation", ticket_identity=identity
    )
    records = [
        json.loads(path.read_text())
        for path in (tmp_path / "acceptance/evidence").glob("*/record.json")
    ]
    assert len(records) == 2
    assert {row["criterion"] for row in records} == {"first", "second"}
    assert len(state.acceptance_transactions) == 1


def test_amendment_retires_old_acceptance_selection_but_keeps_immutable_snapshot(
    tmp_path: Path,
) -> None:
    import json

    from booley.ticket_board.acceptance_ledger import read_acceptance
    from booley.ticket_board.persistence import atomic_replace_bytes

    _root, ticket, tio = _blocked_ticket(tmp_path)
    log = tio.logs_dir / "blocked-again"
    snapshot = log / "ticket.md"
    snapshot.write_bytes(ticket.read_bytes())
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    old = tio.load_basis("blocked-again").ticket_identity()
    record_changes(
        log,
        state,
        state.set_criterion("review_rtl_bugs_clean", True),
        invocation_id="old",
        producer="review",
        execution_id="old",
        ticket_identity=old,
    )
    state.save()
    frozen = freeze_acceptance(
        log,
        state,
        execution_id="old",
        ticket_identity=old,
        participant_heads={"outer": old["baseline"]["outer"]["commit"]},
    )
    for pointer in (log / "review/entry.json", log / ".runtime/triage-prep/manifest.json"):
        atomic_replace_bytes(
            pointer,
            (
                json.dumps({"ticket_identity": old, "ticket_generation": old["generation"]}) + "\n"
            ).encode(),
        )
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert read_acceptance(log).kind != "accepted"
    assert not (log / "review/entry.json").exists()
    assert not (log / ".runtime/triage-prep/manifest.json").exists()
    assert (log / "acceptance/snapshots" / f"{frozen.digest}.json").exists()
    assert (log / "amendments" / f"{result['operation_id']}.acceptance-accepted.json").exists()
    current = tio.load_basis("blocked-again").ticket_identity()
    rebuilt = DevelopmentState.load(state._file_path)
    assert not rebuilt.criteria[
        "review_rtl_bugs_clean"
    ].met  # untrusted mutable proof never carries
    record_changes(
        log,
        rebuilt,
        rebuilt.set_criterion("review_rtl_bugs_clean", True),
        invocation_id="current",
        producer="review",
        execution_id="new",
        ticket_identity=current,
    )
    newer = freeze_acceptance(
        log,
        rebuilt,
        execution_id="new",
        ticket_identity=current,
        participant_heads={"outer": current["baseline"]["outer"]["commit"]},
    )
    assert newer.digest != frozen.digest
    assert len(newer.evidence) == 1


@pytest.mark.asyncio
async def test_successful_old_amendment_runtime_is_repaired_without_pending_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from booley.harness.setup.intake import run
    from booley.runtime.project_dir import reset_cache
    from booley.ticket_board.lifecycle import TicketState

    root, ticket, tio = _blocked_ticket(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    reset_cache()
    snapshot = tio.logs_dir / "blocked-again/ticket.md"
    old_bytes = ticket.read_bytes()
    old = tio.load_basis("blocked-again").ticket_identity()
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    transaction = "c" * 64
    record_changes(
        snapshot.parent,
        state,
        state.set_criterion("review_rtl_bugs_clean", True),
        invocation_id="old",
        producer="review",
        execution_id="old",
        ticket_identity=old,
        transaction_id=transaction,
    )
    state.acceptance_transactions = [transaction]
    state.save()
    tio.move_and_update("blocked-again", TicketState.BLOCKED, {"steps_completed": ["setup"]})
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])
    snapshot.write_bytes(old_bytes)  # reproduce the prior publisher's successful stale snapshot
    state.acceptance_transactions = [transaction]
    state.save()
    history = snapshot.parent / "amendments" / f"{result['operation_id']}.json"
    row = json.loads(history.read_text())
    row.pop("old_ticket_identity")
    row.pop("new_ticket_identity")
    history.write_text(
        json.dumps(row)
    )  # legacy history binds identity through verified old_basis_id
    ctx = await run(str(snapshot), root)
    current = tio.load_basis(ctx.slug, runtime_ticket_path=snapshot).ticket_identity()
    rebuilt = DevelopmentState.load(state._file_path)
    assert current != old
    assert rebuilt.acceptance_transactions == []
    assert rebuilt.criteria["review_rtl_bugs_clean"].mandatory is False
    assert not rebuilt.criteria["review_rtl_bugs_clean"].met
    assert (history.parent / f"{result['operation_id']}.repair.prior-state.json").exists()
