"""Tests for post-developer blocked-ticket dossiers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.core.models import AgentResult
from booley.criteria.state import DevelopmentState
from booley.harness import blocked_prep as bp
from booley.harness import developer, terminal
from booley.harness.models import TicketContext


def _context(tmp_path: Path) -> bp.BlockedContext:
    log_dir = tmp_path / "logs" / "demo"
    runtime = log_dir / ".runtime" / "triage-prep"
    ticket = tmp_path / "blocked" / "demo.md"
    ticket.parent.mkdir()
    ticket.write_text("ticket\n", encoding="utf-8")
    return bp.BlockedContext(tmp_path, "demo", ticket, log_dir, runtime, None)


def _diagnosis() -> dict:
    return {
        "classification": "ticket-code",
        "board_reason": "simulation failed",
        "blocked_stage": "developer",
        "blockers": [{"name": "sim_pass", "reason": "one test failed", "evidence": "state"}],
        "passing_non_blocking": ["lint_clean"],
        "developer_questions": [],
        "recommended_action": "unblock with feedback after fixing the test",
        "findings": [],
    }


@pytest.mark.asyncio
async def test_prepare_blocked_dossier_persists_agent_diagnosis(tmp_path: Path, monkeypatch):
    plain = _context(tmp_path)
    ctx = bp.BlockedContext(
        plain.project_root,
        plain.slug,
        plain.ticket_path,
        plain.log_dir,
        plain.runtime_dir,
        plain.worktree,
        authored_drift=True,
        authored_drift_reason="acceptance-input-change-required: authored Ticket changed",
    )

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis(), cost_usd=0.02)

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)

    outcome = await bp.prepare_blocked_dossier(tmp_path, "demo")

    assert outcome.ready
    package = json.loads(outcome.package_path.read_text(encoding="utf-8"))
    assert package["diagnosis"]["blockers"][0]["name"] == "sim_pass"
    assert package["authored_drift"] is True
    assert package["authored_drift_reason"] == ctx.authored_drift_reason
    manifest = json.loads((ctx.runtime_dir / "blocked-manifest.json").read_text())
    assert manifest["source_inputs"]["version"] == 2
    assert "ticket" in manifest["source_inputs"]["records"]
    assert manifest["cost_usd"] == 0.02


@pytest.mark.asyncio
async def test_render_blocked_dossier_is_check_only(tmp_path: Path, monkeypatch):
    plain = _context(tmp_path)
    ctx = bp.BlockedContext(
        plain.project_root,
        plain.slug,
        plain.ticket_path,
        plain.log_dir,
        plain.runtime_dir,
        plain.worktree,
        authored_drift=True,
        authored_drift_reason="acceptance-input-change-required: authored Ticket changed",
    )

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis(), cost_usd=0.02)

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready

    outcome = bp.render_blocked_dossier(tmp_path, "demo")

    assert outcome.ready
    assert "**Blocked by:**" in outcome.message
    assert "**Authored drift:**" in outcome.message
    assert "use return-to-draft" in outcome.message
    assert "**sim_pass — one test failed.**" in outcome.message
    assert "**Passing / non-blocking:** lint_clean" in outcome.message


@pytest.mark.asyncio
async def test_board_show_blocked_names_changed_dossier_input(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis(), cost_usd=0.02)

    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready
    ctx.ticket_path.write_text("changed ticket\n", encoding="utf-8")

    outcome = bp.render_blocked_dossier(tmp_path, "demo")

    assert outcome.status == "stale"
    assert outcome.message == "blocked dossier is stale: ticket changed"


@pytest.mark.asyncio
async def test_native_finalization_keeps_flushed_blocked_dossier_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    run_log = ctx.log_dir / "human-logs" / "run.log"
    run_log.parent.mkdir(parents=True)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "demo"
    state.init_criteria({"sim_pass": True})
    state.save()
    ticket_ctx = TicketContext(
        slug="demo",
        ticket_path=ctx.ticket_path,
        ticket_type="feature",
        branch="main",
        summary="demo",
        project_root=tmp_path,
    )
    captured: dict[str, Path] = {}

    async def invoke(_ctx, evidence):
        captured.update(dict(evidence))
        return AgentResult(structured=_diagnosis(), cost_usd=0.02)

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    monkeypatch.setattr(developer.ticket_cli, "ticket_status", lambda *_args: "blocked")
    monkeypatch.setattr(developer, "_status_step", lambda *_args: "developer")
    monkeypatch.setattr(developer, "_write_status", lambda *_args: None)

    terminal.open_log(run_log)
    try:
        terminal.raw("blocked before final totals")
        await developer._prepare_blocked_triage(ticket_ctx, tmp_path)
        terminal.raw("final totals")
    finally:
        terminal.close_log()

    outcome = bp.render_blocked_dossier(tmp_path, "demo")
    assert outcome.ready
    assert captured["run_log"].read_text(encoding="utf-8") == "blocked before final totals\n"
    saved = DevelopmentState.load(state_path)
    assert saved.timeline[-1]["mcp_tool"] == "triage_report"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "relative"),
    [
        ("blocked_log", "blocked.md"),
        ("developer_report", "REPORT.md"),
        ("transitions", "human-logs/transitions.log"),
    ],
)
async def test_render_names_changed_exact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, relative: str
) -> None:
    ctx = _context(tmp_path)
    path = ctx.log_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before\n", encoding="utf-8")

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready

    path.write_text("after\n", encoding="utf-8")

    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        f"blocked dossier is stale: {label} changed"
    )


@pytest.mark.asyncio
async def test_render_checks_snapshot_and_package_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    run_log = ctx.log_dir / "human-logs" / "run.log"
    run_log.parent.mkdir(parents=True)
    run_log.write_text("blocked\n", encoding="utf-8")

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    outcome = await bp.prepare_blocked_dossier(tmp_path, "demo")
    manifest = json.loads((ctx.runtime_dir / "blocked-manifest.json").read_text())
    snapshot = Path(manifest["source_inputs"]["records"]["run_log"]["snapshot_path"])

    snapshot.write_text("tampered\n", encoding="utf-8")
    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: run_log evidence integrity changed"
    )
    snapshot.unlink()
    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: run_log evidence missing"
    )
    snapshot.write_text("blocked\n", encoding="utf-8")
    outcome.package_path.write_text("{}\n", encoding="utf-8")
    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: package integrity changed"
    )


@pytest.mark.asyncio
async def test_old_manifest_version_requires_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    ctx.runtime_dir.mkdir(parents=True)
    (ctx.runtime_dir / "blocked-manifest.json").write_text(
        json.dumps({"version": 1, "status": "ready"}), encoding="utf-8"
    )
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)

    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: manifest version changed"
    )


def test_git_failure_names_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    ctx = bp.BlockedContext(
        ctx.project_root, ctx.slug, ctx.ticket_path, ctx.log_dir, ctx.runtime_dir, worktree
    )
    monkeypatch.setattr(
        bp.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b"", b"bad head"),
    )

    with pytest.raises(RuntimeError, match="worktree/head collection failed: bad head"):
        bp._collect_live_inputs(ctx)


def test_git_timeout_names_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(bp.subprocess, "run", time_out)

    with pytest.raises(RuntimeError, match="worktree/head collection timed out"):
        bp._run_git(tmp_path, "worktree/head", "rev-parse", "HEAD")


def test_state_projection_names_malformed_json() -> None:
    with pytest.raises(RuntimeError, match="state input is malformed"):
        bp._state_projection(b"{")


def _context_with_worktree(tmp_path: Path) -> tuple[bp.BlockedContext, Path]:
    plain = _context(tmp_path)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
    source = worktree / "source.txt"
    source.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
    return (
        bp.BlockedContext(
            plain.project_root,
            plain.slug,
            plain.ticket_path,
            plain.log_dir,
            plain.runtime_dir,
            worktree,
        ),
        source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("head", "worktree/head changed"),
        ("tracked", "worktree/diff changed"),
        ("untracked-add", "worktree/untracked/new.txt added"),
        ("untracked-remove", "worktree/untracked/note.txt missing"),
        ("untracked-change", "worktree/untracked/note.txt changed"),
    ],
)
async def test_render_names_worktree_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected: str,
) -> None:
    ctx, source = _context_with_worktree(tmp_path)
    note = ctx.worktree / "note.txt"
    if change in {"untracked-remove", "untracked-change"}:
        note.write_text("before\n", encoding="utf-8")

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready

    if change == "head":
        source.write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.txt"], cwd=ctx.worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "next"], cwd=ctx.worktree, check=True)
    elif change == "tracked":
        source.write_text("dirty\n", encoding="utf-8")
    elif change == "untracked-add":
        (ctx.worktree / "new.txt").write_text("new\n", encoding="utf-8")
    elif change == "untracked-remove":
        note.unlink()
    else:
        note.write_text("after\n", encoding="utf-8")

    outcome = bp.render_blocked_dossier(tmp_path, "demo")
    assert outcome.status == "stale"
    assert expected in outcome.message


@pytest.mark.asyncio
async def test_render_names_semantic_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({"sim_pass": True})
    state.save()

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready
    state = DevelopmentState.load(state_path)
    state.set_criterion("sim_pass", True)
    state.save()

    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: state changed"
    )


@pytest.mark.asyncio
async def test_malformed_source_manifest_and_missing_package_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    outcome = await bp.prepare_blocked_dossier(tmp_path, "demo")
    manifest_path = ctx.runtime_dir / "blocked-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_inputs"] = []
    bp._write_json(manifest_path, manifest)
    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: manifest malformed"
    )

    manifest["source_inputs"] = (await _prepared_inputs(ctx, tmp_path)).to_dict()
    bp._write_json(manifest_path, manifest)
    outcome.package_path.unlink()
    assert bp.render_blocked_dossier(tmp_path, "demo").message == (
        "blocked dossier is stale: package missing"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda source: source.update(version=1), "manifest version changed"),
        (
            lambda source: source["records"]["ticket"].update(comparison="snapshot"),
            "manifest source ticket has invalid comparison",
        ),
        (
            lambda source: source.update(aggregate_sha256="0" * 64),
            "manifest source aggregate changed",
        ),
        (
            lambda source: source["records"]["ticket"].update(snapshot_path=7),
            "manifest malformed",
        ),
    ],
)
async def test_manifest_validation_errors_are_named_and_force_regenerates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    expected: str,
) -> None:
    ctx = _context(tmp_path)

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready
    manifest_path = ctx.runtime_dir / "blocked-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    mutate(manifest["source_inputs"])
    bp._write_json(manifest_path, manifest)

    assert expected in bp.render_blocked_dossier(tmp_path, "demo").message
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo", force=True)).ready


def test_nested_untracked_repository_is_a_stable_named_input(tmp_path: Path) -> None:
    ctx, _source = _context_with_worktree(tmp_path)
    nested = ctx.worktree / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=nested, check=True)

    inputs = bp._collect_live_inputs(ctx)

    assert "worktree/untracked/nested/" in inputs.records


async def _prepared_inputs(ctx: bp.BlockedContext, tmp_path: Path) -> bp.SourceInputs:
    outcome = await bp.prepare_blocked_dossier(tmp_path, "demo", force=True)
    assert outcome.ready
    manifest = json.loads((ctx.runtime_dir / "blocked-manifest.json").read_text())
    return bp._manifest_inputs(manifest["source_inputs"])


def test_failed_manifest_is_failure_not_stale(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    ctx.runtime_dir.mkdir(parents=True)
    (ctx.runtime_dir / "blocked-manifest.json").write_text(
        json.dumps(
            {
                "version": bp.BLOCKED_PACKAGE_VERSION,
                "status": "failed",
                "error": "RuntimeError: worktree/head collection failed: not a git repository",
            }
        )
    )
    outcome = bp.render_blocked_dossier(tmp_path, "demo")
    assert outcome.status == "failed"
    assert "worktree/head collection failed" in outcome.message


@pytest.mark.asyncio
async def test_live_git_collection_failure_is_failed_and_retry_recovers(
    tmp_path: Path, monkeypatch
):
    ctx = _context(tmp_path)
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_diagnosis())

    monkeypatch.setattr(bp, "_invoke", invoke)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready
    collect = bp._collect_live_inputs

    def broken(_ctx):
        raise RuntimeError("worktree/head: not a git repository")

    monkeypatch.setattr(bp, "_collect_live_inputs", broken)
    failed = bp.render_blocked_dossier(tmp_path, "demo")
    assert failed.status == "failed"
    assert "worktree/head" in failed.message
    assert "after resolving" in failed.message.lower()
    monkeypatch.setattr(bp, "_collect_live_inputs", collect)
    assert (await bp.prepare_blocked_dossier(tmp_path, "demo")).ready


@pytest.mark.parametrize("completed", [False, True])
def test_blocked_snapshot_uses_effective_report_gate(tmp_path, completed):
    from booley.ticket_board import report_submission as rs

    ctx = _context(tmp_path)
    state = DevelopmentState.load(ctx.log_dir / ".runtime/booley_state.json")
    state.init_criteria({rs.KEY: True})
    attempt = rs.Submission(ctx.log_dir, "a" * 32, {}, "execution")
    attempt.stage(b"candidate")
    state.set_criterion(
        rs.KEY, True, detail={rs.ID_KEY: "a" * 32, rs.DIGEST_KEY: attempt.row["report_sha256"]}
    )
    state.save()
    if completed:
        attempt.commit()
    attempt.close()
    labels = dict(bp._evidence_paths(ctx))
    assert ("developer_report" in labels) is completed
    raw = bp._evidence_bytes(ctx, "state", labels["state"])
    assert json.loads(raw)["criteria"][rs.KEY]["met"] is completed
