"""Tests for the precomputed rich HTML explanation."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from booley.core.models import AgentResult
from booley.harness import review_prep as rp


def _explanation() -> dict:
    return {
        "background": [{"title": "Existing system", "body": "Grounded background."}],
        "intuition": [{"title": "Core idea", "body": "A concrete toy example."}],
        "code_references": [
            {
                "repository": "rtl",
                "path": "rtl/core.sv",
                "revision": "b" * 40,
                "summary": "The change keeps the invariant explicit.",
            }
        ],
        "findings": [{"title": "Edge case", "detail": "Exited owners remain distinguishable."}],
        "quiz": [
            {
                "question": f"Question {index}?",
                "choices": [
                    {"text": "Correct", "correct": True, "feedback": "Yes"},
                    {"text": "Incorrect", "correct": False, "feedback": "No"},
                ],
            }
            for index in range(5)
        ],
    }


def _ctx(tmp_path: Path) -> rp.ReviewPrepContext:
    log_dir = tmp_path / "logs" / "demo"
    return rp.ReviewPrepContext(
        project_root=tmp_path,
        slug="demo",
        log_dir=log_dir,
        runtime_dir=log_dir / ".runtime" / "triage-prep",
        worktree=tmp_path / "worktree",
        ticket_path=tmp_path / "ticket.md",
        base_sha="a" * 40,
        head_sha="b" * 40,
        feature_branch="demo",
    )


def _git_evidence(path: Path) -> dict[str, Path]:
    return dict.fromkeys(("diff", "commits", "files", "status"), path)


def _assessment() -> dict:
    return {
        "recommendation": "approve",
        "reason": "all mandatory criteria passed",
        "decision_blockers": [],
        "scope_deviations": [],
        "developer_summary": "implemented the ticket",
        "uncertainties": "none",
        "optional_omissions": "none",
        "findings": [],
    }


def _facts() -> dict:
    return {
        "version": 2,
        "kind": "review",
        "slug": "demo",
        "feature_branch": "demo",
        "repositories": [
            {
                "name": "rtl",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "worktree": "/tmp/worktree",
            }
        ],
        "scope": {"deviations": []},
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": "/tmp/REPORT.md",
        "run_economics": "unavailable",
        "health": {},
    }


def test_resolve_context_accepts_blocked_ticket(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "worktree"
    checkout.mkdir()

    class FakeTicketIO:
        logs_dir = tmp_path / "logs"

        def find_ticket(self, _slug):
            return {
                "status": "blocked",
                "file": "board/blocked/demo.md",
                "feature_branch": "demo",
                "base_sha": "a" * 40,
            }

    monkeypatch.setattr(rp, "tickets_dir_from_project_root", lambda _root: tmp_path / "tickets")
    monkeypatch.setattr(rp, "TicketIO", lambda *_args, **_kwargs: FakeTicketIO())
    monkeypatch.setattr(rp, "_find_checkout", lambda *_args: checkout)
    monkeypatch.setattr(
        rp,
        "_git",
        lambda _worktree, *args: "b" * 40 if args[:2] == ("rev-parse", "HEAD") else "",
    )

    ctx = rp._resolve_context(tmp_path, "demo", require_review=True)

    assert ctx.slug == "demo"
    assert ctx.worktree == checkout.resolve()
    assert ctx.ticket_path == tmp_path / "tickets" / "board/blocked/demo.md"


def test_write_output_normalizes_empty_fields_and_missing_scope_rows(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assessment = _assessment()
    assessment["optional_omissions"] = ""
    facts = {"version": 1, "scope": {"deviations": ["rtl/outside.sv"]}}

    prepared = rp._write_output(
        ctx,
        {"explanation": _explanation(), "assessment": assessment},
        facts,
    )

    assert prepared.explanation is not None
    assert prepared.assessment["optional_omissions"] == "none"
    assert prepared.assessment["recommendation"] == "hold"
    assert prepared.assessment["scope_deviations"][0]["path"] == "rtl/outside.sv"
    assert prepared.assessment["scope_deviations"][0]["classification"] == "Needs review"


def test_write_output_falls_back_when_structured_response_is_missing(tmp_path: Path):
    facts = {"version": 1, "scope": {"deviations": ["rtl/outside.sv"]}}

    prepared = rp._write_output(_ctx(tmp_path), None, facts)

    assert prepared.explanation is None
    assert prepared.html_error
    assert prepared.assessment_error
    assert prepared.assessment["recommendation"] == "hold"
    assert prepared.assessment["scope_deviations"] == [
        {
            "path": "rtl/outside.sv",
            "classification": "Needs review",
            "reason": "The report agent did not return a usable assessment.",
        }
    ]


def test_output_schema_contains_structured_explanation_not_html():
    schema = rp._output_schema()

    assert "explanation" in schema["properties"]
    assert "html" not in schema["properties"]
    assert schema["properties"]["explanation"]["properties"]["quiz"]["minItems"] == 5


def test_fresh_outcome_requires_matching_identity_and_files(tmp_path: Path):
    ctx = _ctx(tmp_path)
    html = ctx.log_dir / "explanation.html"
    html.parent.mkdir(parents=True)
    html.write_text("ready", encoding="utf-8")
    briefing = ctx.runtime_dir / "briefing.json"
    briefing.parent.mkdir(parents=True)
    briefing.write_text("{}", encoding="utf-8")
    manifest = {
        "status": "ready",
        "version": rp._PROMPT_VERSION,
        "prompt_sha256": "prompt",
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "source_sha256": "source",
        "html_path": str(html),
        "html_sha256": rp._file_sha256(html),
        "briefing_path": str(briefing),
        "briefing_sha256": rp._file_sha256(briefing),
    }

    outcome = rp._fresh_outcome(ctx, manifest, "prompt", "source")
    assert outcome is not None and outcome.status == "fresh"

    manifest["head_sha"] = "c" * 40
    assert rp._fresh_outcome(ctx, manifest, "prompt", "source") is None

    manifest["head_sha"] = ctx.head_sha
    html.write_text("changed", encoding="utf-8")
    assert rp._fresh_outcome(ctx, manifest, "prompt", "source") is None

    manifest["html_path"] = None
    manifest.pop("html_sha256")
    manifest["html_error"] = "ReviewPrepError: HTML is incomplete"
    outcome = rp._fresh_outcome(ctx, manifest, "prompt", "source")
    assert outcome is not None and outcome.status == "fresh"
    assert outcome.html_path is None


@pytest.mark.asyncio
async def test_agent_invocation_is_read_only(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    captured = []

    async def call(params):
        captured.append(params)
        return AgentResult()

    config = SimpleNamespace(
        model_for_role=lambda *_args: "review-model",
        effort_for_tier=lambda *_args: "medium",
    )
    monkeypatch.setattr(rp, "get_backend_config", lambda: config)
    monkeypatch.setattr(rp, "call_agent", call)
    repository = tmp_path / "snapshot"
    repository.mkdir()
    workspace = rp.ReviewAgentWorkspace(repository, {"diff": tmp_path / "copy.diff"})

    await rp._invoke_agent(ctx, "exact prompt", workspace)

    assert captured[0].allowed_agent_capabilities == ["Read", "Glob", "Grep"]
    assert captured[0].nested_mcp_tools == []
    assert captured[0].model == "review-model"
    assert captured[0].cwd == repository
    assert str(ctx.worktree) not in captured[0].prompt


def test_agent_workspace_is_a_disposable_snapshot(tmp_path: Path):
    worktree = tmp_path / "live"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
    source = worktree / "source.txt"
    source.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "fixture"], check=True)
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ctx = replace(_ctx(tmp_path), worktree=worktree, head_sha=head)
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")

    evidence = ctx.runtime_dir / "git-evidence.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("evidence\n", encoding="utf-8")
    package = rp._build_evidence_package(ctx, "source", _git_evidence(evidence))

    with rp._agent_workspace(ctx, package) as workspace:
        snapshot_source = workspace.repository / "source.txt"
        assert snapshot_source.read_text(encoding="utf-8") == "committed\n"
        snapshot_source.write_text("agent edit\n", encoding="utf-8")
        assert workspace.evidence["ticket"].read_text(encoding="utf-8") == "ticket\n"

    assert source.read_text(encoding="utf-8") == "committed\n"


def test_find_checkout_uses_supplied_project_root(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "feature"
    calls = []

    def git(root, *args, **_kwargs):
        calls.append((root, args))
        return f"worktree {checkout}\nHEAD {'a' * 40}\nbranch refs/heads/demo\n\n"

    monkeypatch.setattr(rp, "_git", git)

    assert rp._find_checkout(tmp_path, "demo") == checkout.resolve()
    assert calls == [(tmp_path, ("worktree", "list", "--porcelain"))]


def test_source_fingerprint_changes_when_run_evidence_changes(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")
    report = ctx.log_dir / "REPORT.md"
    report.parent.mkdir(parents=True)
    report.write_text("first\n", encoding="utf-8")
    monkeypatch.setattr(rp, "_git", lambda *_args, **_kwargs: "")

    before = rp._source_fingerprint(ctx)
    report.write_text("second\n", encoding="utf-8")

    assert rp._source_fingerprint(ctx) != before


def test_source_fingerprint_ignores_human_logs_written_by_review_prep(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(rp, "_git", lambda *_args, **_kwargs: "")

    before = rp._source_fingerprint(ctx)
    human_logs = ctx.log_dir / "human-logs"
    human_logs.mkdir(parents=True)
    (human_logs / "run.log").write_text("triage-report started\n", encoding="utf-8")
    (human_logs / "harness.log").write_text("triage-report done\n", encoding="utf-8")

    assert rp._source_fingerprint(ctx) == before


@pytest.mark.asyncio
async def test_stable_context_reresolves_after_ticket_jobs_drain(tmp_path: Path, monkeypatch):
    before = _ctx(tmp_path)
    after = replace(before, head_sha="c" * 40)
    contexts = iter([before, after])
    wait = AsyncMock(return_value=[SimpleNamespace(tool="mutation_tester")])
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args: next(contexts))
    monkeypatch.setattr(rp, "wait_for_ticket_jobs", wait)

    resolved = await rp._resolve_stable_context(tmp_path, "demo")

    assert resolved is after
    wait.assert_awaited_once_with(before.log_dir)


@pytest.mark.asyncio
async def test_prepare_review_writes_package_and_manifest(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    evidence = ctx.runtime_dir / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("evidence", encoding="utf-8")
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")

    async def invoke(*_args, **_kwargs):
        return AgentResult(
            structured={"explanation": _explanation(), "assessment": _assessment()},
            cost_usd=0.125,
        )

    @contextmanager
    def workspace(_ctx, _evidence):
        yield rp.ReviewAgentWorkspace(ctx.worktree, {"diff": evidence})

    monkeypatch.setattr(rp, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(rp, "_prompt_text", lambda: "exact prompt")
    monkeypatch.setattr(rp, "_collect_git_evidence", lambda _ctx: _git_evidence(evidence))
    monkeypatch.setattr(rp, "build_review_facts", lambda _ctx: _facts())
    monkeypatch.setattr(rp, "_source_fingerprint", lambda _ctx: "source")
    monkeypatch.setattr(rp, "_agent_workspace", workspace)
    monkeypatch.setattr(rp, "_invoke_agent", invoke)
    monkeypatch.setattr(rp, "_record_call", lambda *_args, **_kwargs: None)
    config = SimpleNamespace(model_for_role=lambda *_args: "review-model")
    monkeypatch.setattr(rp, "get_backend_config", lambda: config)

    outcome = await rp.prepare_review(tmp_path, "demo")

    assert outcome.status == "ready"
    assert outcome.html_path and outcome.html_path.is_file()
    manifest = json.loads((ctx.runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "ready"
    assert manifest["head_sha"] == ctx.head_sha
    assert manifest["source_sha256"] == "source"
    assert manifest["html_sha256"] == rp._file_sha256(outcome.html_path)
    assert Path(manifest["briefing_path"]).is_file()
    assert manifest["briefing_sha256"] == rp._file_sha256(Path(manifest["briefing_path"]))
    assert manifest["cost_usd"] == 0.125
    evidence_manifest = json.loads(
        (ctx.runtime_dir / "evidence-manifest.json").read_text(encoding="utf-8")
    )
    assert evidence_manifest["version"] == 1
    assert evidence_manifest["head_sha"] == ctx.head_sha
    assert {item["name"] for item in evidence_manifest["items"]} == {
        "commits",
        "diff",
        "files",
        "status",
        "ticket",
        "triage_facts",
    }

    assert rp.verify_review_handoff(tmp_path, "demo").ready
    briefing_path = Path(manifest["briefing_path"])
    briefing_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(rp.ReviewPrepError, match="no current"):
        rp.verify_review_handoff(tmp_path, "demo")


def test_source_fingerprint_survives_ticket_handoff(tmp_path: Path, monkeypatch):
    running = _ctx(tmp_path)
    review = replace(running, ticket_path=tmp_path / "review" / "ticket.md")
    running.worktree.mkdir()
    running.ticket_path.write_text("same ticket\n", encoding="utf-8")
    review.ticket_path.parent.mkdir()
    review.ticket_path.write_text("same ticket\n", encoding="utf-8")
    monkeypatch.setattr(rp, "_git", lambda *_args, **_kwargs: "")

    assert rp._source_fingerprint(running) == rp._source_fingerprint(review)


def test_review_briefing_command_uses_prepared_package_only(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    package_path = ctx.runtime_dir / "briefing.json"
    package_path.parent.mkdir(parents=True)
    package = {
        "version": 2,
        "repositories": [
            {
                "name": "rtl",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "worktree": str(tmp_path / "worktree"),
            }
        ],
        "slug": "demo",
        "assessment": _assessment(),
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": str(ctx.log_dir / "explanation.html"),
        "run_economics": "tokens=10 cost=$0.01",
        "health": {},
    }
    package_path.write_text(json.dumps(package), encoding="utf-8")
    manifest = {"briefing_path": str(package_path)}
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(rp, "_prompt_text", lambda: "prompt")
    monkeypatch.setattr(rp, "_source_fingerprint", lambda _ctx: "source")
    monkeypatch.setattr(rp, "_read_manifest", lambda _ctx: manifest)
    monkeypatch.setattr(
        rp,
        "_fresh_outcome",
        lambda *_args: rp.ReviewPrepOutcome("fresh", "current", Path(package["html_path"])),
    )
    opened = []
    monkeypatch.setattr(rp, "open_package_diffs", lambda value: opened.append(value) or [])

    outcome = rp.review_briefing_command(tmp_path, "demo")

    assert outcome.status == "ready"
    assert "**Recommendation:** approve" in outcome.briefing
    assert len(opened) == 1
    assert opened[0]["slug"] == package["slug"]
    assert opened[0].version == 2


def test_review_briefing_command_supports_report_disabled_ticket(tmp_path: Path, monkeypatch):
    ctx = replace(_ctx(tmp_path), triage_report_enabled=False)
    facts = {
        "version": 2,
        "kind": "review",
        "slug": "demo",
        "feature_branch": "demo",
        "repositories": [
            {
                "name": "rtl",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "worktree": str(ctx.worktree),
            }
        ],
        "scope": {"decidable": True, "deviations": ["rtl/extra.sv"]},
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "run_economics": "tokens=10 cost=$0.01",
        "health": {},
    }
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(rp, "build_review_facts", lambda _ctx: facts)

    outcome = rp.review_briefing_command(tmp_path, "demo", open_diffs=False)

    assert outcome.status == "ready"
    assert "**Recommendation:** hold" in outcome.briefing
    assert "`rtl/extra.sv` — **Needs review**" in outcome.briefing
    assert "HTML explanation: unavailable" in outcome.briefing


@pytest.mark.asyncio
async def test_prepare_review_keeps_briefing_when_html_is_invalid(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    evidence = ctx.runtime_dir / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("evidence", encoding="utf-8")
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")

    async def invoke(*_args, **_kwargs):
        return AgentResult(structured={"explanation": {"quiz": []}, "assessment": _assessment()})

    @contextmanager
    def workspace(_ctx, _evidence):
        yield rp.ReviewAgentWorkspace(ctx.worktree, {"diff": evidence})

    calls: list[tuple[AgentResult | None, int]] = []
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(rp, "_prompt_text", lambda: "exact prompt")
    monkeypatch.setattr(rp, "_collect_git_evidence", lambda _ctx: _git_evidence(evidence))
    monkeypatch.setattr(rp, "build_review_facts", lambda _ctx: _facts())
    monkeypatch.setattr(rp, "_source_fingerprint", lambda _ctx: "source")
    monkeypatch.setattr(rp, "_agent_workspace", workspace)
    monkeypatch.setattr(rp, "_invoke_agent", invoke)
    monkeypatch.setattr(
        rp,
        "_record_call",
        lambda _ctx, result, _duration, *, exit_code: calls.append((result, exit_code)),
    )

    outcome = await rp.prepare_review(tmp_path, "demo")

    assert outcome.status == "ready"
    assert outcome.html_path is None
    assert len(calls) == 1
    assert calls[0][0] is not None
    assert calls[0][1] == 0
    manifest = json.loads((ctx.runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "ready"
    assert manifest["html_path"] is None
    assert "background must be a list" in manifest["html_error"]
    briefing = json.loads(Path(manifest["briefing_path"]).read_text(encoding="utf-8"))
    assert briefing["html_path"] is None
    assert any(
        "HTML explanation unavailable" in item for item in briefing["assessment"]["findings"]
    )
    briefing_outcome = rp.review_briefing_command(tmp_path, "demo", open_diffs=False)
    assert briefing_outcome.status == "ready"
    assert "HTML explanation: unavailable" in briefing_outcome.briefing


@pytest.mark.asyncio
async def test_prepare_review_persists_prompt_setup_failure(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(
        rp,
        "_prompt_text",
        lambda: (_ for _ in ()).throw(rp.ReviewPrepError("prompt missing")),
    )
    monkeypatch.setattr(rp, "_source_fingerprint", lambda _ctx: "source")
    monkeypatch.setattr(rp, "_record_call", lambda *_args, **_kwargs: None)

    outcome = await rp.prepare_review(tmp_path, "demo")

    assert outcome.status == "failed"
    manifest = json.loads((ctx.runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "prompt missing" in manifest["error"]


@pytest.mark.asyncio
async def test_prepare_review_marks_live_input_changes_concurrent(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir()
    evidence = ctx.runtime_dir / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("evidence", encoding="utf-8")
    ctx.ticket_path.write_text("ticket\n", encoding="utf-8")

    invoked = False

    async def invoke(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return AgentResult(structured={"explanation": _explanation()})

    @contextmanager
    def workspace(_ctx, _evidence):
        yield rp.ReviewAgentWorkspace(ctx.worktree, {"diff": evidence})

    fingerprints = iter(["before", "before", "after", "after"])
    monkeypatch.setattr(rp, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(rp, "_prompt_text", lambda: "exact prompt")
    monkeypatch.setattr(rp, "_collect_git_evidence", lambda _ctx: _git_evidence(evidence))
    monkeypatch.setattr(rp, "build_review_facts", lambda _ctx: _facts())
    monkeypatch.setattr(rp, "_source_fingerprint", lambda _ctx: next(fingerprints))
    monkeypatch.setattr(rp, "_agent_workspace", workspace)
    monkeypatch.setattr(rp, "_invoke_agent", invoke)
    monkeypatch.setattr(
        rp,
        "_record_call",
        lambda *_args, **_kwargs: pytest.fail("concurrent failure must not rewrite state"),
    )
    outcome = await rp.prepare_review(tmp_path, "demo")

    assert outcome.status == "changed"
    assert not invoked
    manifest = json.loads((ctx.runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "changed"
    assert "concurrently" in manifest["error"] or "snapshot" in manifest["error"]
