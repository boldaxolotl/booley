"""Tests for post-developer blocked-ticket dossiers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from booley.core.models import AgentResult
from booley.harness import blocked_prep as bp


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
    ctx = _context(tmp_path)

    async def invoke(_ctx):
        return AgentResult(structured=_diagnosis(), cost_usd=0.02)

    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_source_sha", lambda _ctx: "source")
    monkeypatch.setattr(bp, "_invoke", invoke)

    outcome = await bp.prepare_blocked_dossier(tmp_path, "demo")

    assert outcome.ready
    package = json.loads(outcome.package_path.read_text(encoding="utf-8"))
    assert package["diagnosis"]["blockers"][0]["name"] == "sim_pass"
    manifest = json.loads((ctx.runtime_dir / "blocked-manifest.json").read_text())
    assert manifest["source_sha256"] == "source"
    assert manifest["cost_usd"] == 0.02


def test_render_blocked_dossier_is_check_only(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    path = ctx.runtime_dir / "blocked-briefing.json"
    package = {
        "version": 1,
        "kind": "blocked",
        "slug": "demo",
        "ticket_path": str(ctx.ticket_path),
        "blocked_log_path": str(ctx.log_dir / "blocked.md"),
        "diagnosis": _diagnosis(),
    }
    bp._write_json(path, package)
    bp._write_json(
        ctx.runtime_dir / "blocked-manifest.json",
        {
            "version": 1,
            "status": "ready",
            "source_sha256": "source",
            "package_path": str(path),
            "package_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    )
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_source_sha", lambda _ctx: "source")

    outcome = bp.render_blocked_dossier(tmp_path, "demo")

    assert outcome.ready
    assert "**Blocked by:**" in outcome.message
    assert "**sim_pass — one test failed.**" in outcome.message
    assert "**Passing / non-blocking:** lint_clean" in outcome.message


def test_render_blocked_dossier_rejects_stale_source(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr(bp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(bp, "_source_sha", lambda _ctx: "new-source")

    outcome = bp.render_blocked_dossier(tmp_path, "demo")

    assert outcome.status == "stale"


def test_source_hash_changes_when_dirty_file_content_changes(tmp_path: Path):
    ctx = _context(tmp_path)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
        check=True,
    )
    source = worktree / "source.txt"
    source.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
    source.write_text("first dirty value\n", encoding="utf-8")
    dirty_ctx = bp.BlockedContext(
        ctx.project_root, ctx.slug, ctx.ticket_path, ctx.log_dir, ctx.runtime_dir, worktree
    )
    before = bp._source_sha(dirty_ctx)

    source.write_text("second dirty value\n", encoding="utf-8")

    assert bp._source_sha(dirty_ctx) != before
