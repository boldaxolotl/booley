"""Precompute a concise diagnosis after a developer run leaves a ticket blocked."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.settings import get_backend_config
from booley.core.boundary import require_dict, require_str
from booley.core.models import AgentCallParams, AgentResult
from booley.dev_support.development_state import DevelopmentState
from booley.runtime.agent import call_agent
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.paths import existing_runtime_file, ticket_runtime_dir

logger = logging.getLogger(__name__)

BLOCKED_PACKAGE_VERSION = 1
_CLASSIFICATIONS = frozenset({"harness", "infrastructure", "ticket-code", "mixed", "unknown"})


@dataclass(frozen=True)
class BlockedPrepOutcome:
    """Result of post-run blocked-ticket preparation."""

    status: str
    message: str
    package_path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "fresh"}


@dataclass(frozen=True)
class BlockedContext:
    project_root: Path
    slug: str
    ticket_path: Path
    log_dir: Path
    runtime_dir: Path
    worktree: Path | None


def _find_checkout(project_root: Path, branch: str) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(project_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    checkout: Path | None = None
    wanted = f"refs/heads/{branch}"
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            checkout = Path(line.removeprefix("worktree "))
        elif line == f"branch {wanted}" and checkout is not None:
            return checkout.resolve()
        elif not line:
            checkout = None
    return None


def _resolve_context(project_root: Path, slug: str) -> BlockedContext:
    tickets_dir = tickets_dir_from_project_root(project_root)
    tio = TicketIO(tickets_dir, project_root=project_root)
    entry = tio.find_ticket(slug)
    if not entry:
        raise RuntimeError(f"ticket '{slug}' was not found")
    if entry.get("status") != "blocked":
        raise RuntimeError(f"ticket '{slug}' is {entry.get('status')}, not blocked")
    worktree_value = entry.get("worktree")
    worktree = Path(worktree_value).resolve() if isinstance(worktree_value, str) else None
    feature_branch = str(entry.get("feature_branch") or slug)
    if worktree is None or not worktree.is_dir():
        worktree = _find_checkout(project_root, feature_branch)
    if worktree is None or not worktree.is_dir():
        conventional = project_root / ".booley_project" / "worktrees" / slug
        worktree = conventional.resolve() if conventional.is_dir() else None
    log_dir = tio.logs_dir / slug
    return BlockedContext(
        project_root=project_root,
        slug=slug,
        ticket_path=tickets_dir / str(entry["file"]),
        log_dir=log_dir,
        runtime_dir=ticket_runtime_dir(log_dir) / "triage-prep",
        worktree=worktree,
    )


def _evidence_paths(ctx: BlockedContext) -> list[tuple[str, Path]]:
    candidates = {
        "ticket": ctx.ticket_path,
        "blocked_log": ctx.log_dir / "blocked.md",
        "transitions": ctx.log_dir / "human-logs" / "transitions.log",
        "run_log": ctx.log_dir / "human-logs" / "run.log",
        "state": ctx.log_dir / ".runtime" / "booley_state.json",
        "developer_report": ctx.log_dir / "REPORT.md",
        "developer": ctx.log_dir / ".runtime" / "developer",
        "flow_reports": ctx.log_dir / ".runtime" / "flow-reports",
        "specialist_reports": ctx.log_dir / ".runtime" / "mcp-tool-reports",
    }
    rows: list[tuple[str, Path]] = []
    for label, path in candidates.items():
        if path.is_file() and not path.is_symlink():
            rows.append((label, path))
        elif path.is_dir():
            rows.extend(
                (f"{label}/{child.relative_to(path)}", child)
                for child in sorted(path.rglob("*"))
                if child.is_file() and not child.is_symlink()
            )
    return rows


def _source_sha(ctx: BlockedContext) -> str:
    digest = hashlib.sha256()
    for label, path in _evidence_paths(ctx):
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    if ctx.worktree:
        for args in (
            ("rev-parse", "HEAD"),
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ("diff", "--binary", "HEAD", "--"),
        ):
            result = subprocess.run(
                ["git", "-C", str(ctx.worktree), *args],
                capture_output=True,
                timeout=30,
                check=False,
            )
            digest.update(result.stdout)
        untracked = subprocess.run(
            [
                "git",
                "-C",
                str(ctx.worktree),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        for raw_path in (value for value in untracked.stdout.split(b"\0") if value):
            relative = raw_path.decode(errors="surrogateescape")
            path = ctx.worktree / relative
            digest.update(raw_path)
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(str(path.readlink()).encode(errors="surrogateescape"))
            elif path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _schema() -> dict[str, Any]:
    string_list = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "classification": {"type": "string", "enum": sorted(_CLASSIFICATIONS)},
            "board_reason": {"type": "string"},
            "blocked_stage": {"type": "string"},
            "blockers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "reason": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["name", "reason", "evidence"],
                    "additionalProperties": False,
                },
            },
            "passing_non_blocking": string_list,
            "developer_questions": string_list,
            "recommended_action": {"type": "string"},
            "findings": string_list,
        },
        "required": [
            "classification",
            "board_reason",
            "blocked_stage",
            "blockers",
            "passing_non_blocking",
            "developer_questions",
            "recommended_action",
            "findings",
        ],
        "additionalProperties": False,
    }


def _validate(value: Any) -> dict[str, Any]:
    diagnosis = require_dict(value, field="blocked-ticket diagnosis")
    classification = require_str(diagnosis, "classification")
    if classification not in _CLASSIFICATIONS:
        raise RuntimeError(f"invalid blocked-ticket classification: {classification}")
    for key in ("board_reason", "blocked_stage", "recommended_action"):
        require_str(diagnosis, key)
    blockers = diagnosis.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        raise RuntimeError("blocked-ticket diagnosis must identify at least one blocker")
    for blocker in blockers:
        row = require_dict(blocker, field="blocked-ticket blocker")
        for key in ("name", "reason", "evidence"):
            require_str(row, key)
    for key in ("passing_non_blocking", "developer_questions", "findings"):
        items = diagnosis.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise RuntimeError(f"blocked-ticket diagnosis {key} must be a string list")
    return diagnosis


def _prompt(ctx: BlockedContext) -> str:
    paths = "\n".join(f"- {label}: `{path}`" for label, path in _evidence_paths(ctx))
    worktree = str(ctx.worktree) if ctx.worktree else "not available"
    return f"""Diagnose blocked Booley ticket `{ctx.slug}` after its developer run.

Read the evidence below. Inspect the worktree with read-only Git commands when useful.
Distinguish current blockers from passing checks, stale-but-fixed findings, and warnings.
Report every independent blocker. Preserve the latest board transition reason exactly in
`board_reason`; call out conflicts in findings. Recommend one of investigate, unblock with
feedback, reset, archive, or defer. Do not modify files.

Worktree: `{worktree}`
Evidence:
{paths}
"""


async def _invoke(ctx: BlockedContext) -> AgentResult:
    cfg = get_backend_config()
    return await call_agent(
        AgentCallParams(
            prompt=_prompt(ctx),
            system_prompt=(
                "You are a read-only senior incident reviewer preparing a concise "
                "blocked-ticket triage dossier grounded only in supplied evidence."
            ),
            model=cfg.model_for_role("triage_report", "standard"),
            reasoning_effort=cfg.effort_for_tier("standard"),
            cwd=ctx.worktree or ctx.project_root,
            allowed_agent_capabilities=["Read", "Glob", "Grep"],
            output_format=_schema(),
            max_turns=40,
            timeout_seconds=600,
            transcript_path=ctx.runtime_dir / "blocked-agent.jsonl",
            label="blocked-triage-report",
            nested_mcp_tools=[],
        )
    )


def _package_path(ctx: BlockedContext) -> Path:
    return ctx.runtime_dir / "blocked-briefing.json"


def _manifest_path(ctx: BlockedContext) -> Path:
    return ctx.runtime_dir / "blocked-manifest.json"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _record_call(ctx: BlockedContext, result: AgentResult, duration: float) -> None:
    state_path = existing_runtime_file(ctx.log_dir.parent, ctx.slug, "booley_state.json")
    if not state_path.is_file():
        return
    state = DevelopmentState.load(state_path)
    state.record_mcp_tool_run(
        "triage_report",
        0,
        duration_s=duration,
        cost_usd=result.cost_usd,
    )
    state.save()


def _fresh(ctx: BlockedContext, source_sha: str) -> Path | None:
    try:
        manifest = json.loads(_manifest_path(ctx).read_text(encoding="utf-8"))
        package_path = Path(manifest["package_path"])
        package_hash = hashlib.sha256(package_path.read_bytes()).hexdigest()
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        manifest.get("version") == BLOCKED_PACKAGE_VERSION
        and manifest.get("status") == "ready"
        and manifest.get("source_sha256") == source_sha
        and manifest.get("package_sha256") == package_hash
    ):
        return package_path
    return None


async def prepare_blocked_dossier(project_root: Path, slug: str) -> BlockedPrepOutcome:
    """Prepare a best-effort dossier after a ticket remains blocked."""
    started = time.monotonic()
    try:
        ctx = _resolve_context(project_root.resolve(), slug)
        source_sha = _source_sha(ctx)
        if path := _fresh(ctx, source_sha):
            return BlockedPrepOutcome("fresh", "blocked dossier is current", path)
        result = await _invoke(ctx)
        diagnosis = _validate(result.structured)
        if _source_sha(ctx) != source_sha:
            raise RuntimeError("blocked-ticket evidence changed during diagnosis")
        duration = time.monotonic() - started
        _record_call(ctx, result, duration)
        source_sha = _source_sha(ctx)
        package = {
            "version": BLOCKED_PACKAGE_VERSION,
            "kind": "blocked",
            "slug": slug,
            "ticket_path": str(ctx.ticket_path),
            "blocked_log_path": str(ctx.log_dir / "blocked.md"),
            "diagnosis": diagnosis,
        }
        path = _package_path(ctx)
        _write_json(path, package)
        manifest = {
            "version": BLOCKED_PACKAGE_VERSION,
            "status": "ready",
            "source_sha256": source_sha,
            "package_path": str(path),
            "package_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "duration_s": round(duration, 2),
            "cost_usd": round(result.cost_usd, 4),
            "updated_at": utc_now_rfc3339(),
        }
        _write_json(_manifest_path(ctx), manifest)
        return BlockedPrepOutcome("ready", "blocked dossier prepared", path)
    except Exception as exc:  # noqa: BLE001 - post-processing must never alter disposition
        logger.warning("Blocked dossier preparation failed for %s: %s", slug, exc, exc_info=True)
        if "ctx" in locals():
            try:
                _write_json(
                    _manifest_path(ctx),
                    {
                        "version": BLOCKED_PACKAGE_VERSION,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:2000],
                        "updated_at": utc_now_rfc3339(),
                    },
                )
            except OSError:
                logger.warning("Could not persist blocked dossier failure for %s", slug)
        return BlockedPrepOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])


def render_blocked_dossier(project_root: Path, slug: str) -> BlockedPrepOutcome:
    """Load a current blocked dossier without invoking an agent."""
    try:
        ctx = _resolve_context(project_root.resolve(), slug)
        path = _fresh(ctx, _source_sha(ctx))
        if path is None:
            try:
                manifest = json.loads(_manifest_path(ctx).read_text(encoding="utf-8"))
                error = manifest.get("error")
            except (OSError, json.JSONDecodeError):
                error = None
            detail = f": {error}" if isinstance(error, str) else ""
            return BlockedPrepOutcome("stale", f"blocked dossier is missing or stale{detail}")
        package = json.loads(path.read_text(encoding="utf-8"))
        diagnosis = _validate(package.get("diagnosis"))
        lines = [f"### {slug}", "", "**Blocked by:**", ""]
        for index, blocker in enumerate(diagnosis["blockers"], 1):
            lines.append(
                f"{index}. **{blocker['name']} — {blocker['reason']}.** {blocker['evidence']}"
            )
        passing = "; ".join(diagnosis["passing_non_blocking"]) or "none recorded"
        lines.extend(
            [
                "",
                f"**Board reason:** {diagnosis['board_reason']}",
                f"**Blocked stage:** {diagnosis['blocked_stage']}",
                f"**Classification:** {diagnosis['classification']}",
                f"**Evidence:** [blocked.md]({package['blocked_log_path']}) · "
                f"[ticket]({package['ticket_path']})",
                f"**Passing / non-blocking:** {passing}",
                f"**Recommended action:** {diagnosis['recommended_action']}",
            ]
        )
        if diagnosis["developer_questions"]:
            lines.extend(["", "**Developer questions:**"])
            lines.extend(f"- {item}" for item in diagnosis["developer_questions"])
        if diagnosis["findings"]:
            lines.extend(["", "**Findings:**"])
            lines.extend(f"- {item}" for item in diagnosis["findings"])
        return BlockedPrepOutcome("ready", "\n".join(lines), path)
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns a stable outcome
        return BlockedPrepOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])
