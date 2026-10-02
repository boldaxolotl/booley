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

from booley.core.boundary import (
    as_dict,
    require_dict,
    require_list,
    require_opt_str,
    require_str,
    require_str_value,
)
from booley.core.models import AgentCallParams, AgentResult
from booley.criteria.state import DevelopmentState
from booley.runtime.agent import call_agent
from booley.runtime.agent_config import get_backend_config
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.agent_execution import configure_agent_call
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.paths import existing_runtime_file, ticket_runtime_dir

logger = logging.getLogger(__name__)

BLOCKED_PACKAGE_VERSION = 2
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
    authored_drift: bool = False
    authored_drift_reason: str = ""


@dataclass(frozen=True)
class SourceInputRecord:
    """One stable, independently comparable dossier input."""

    sha256: str
    comparison: str = "exact"
    snapshot_path: str | None = None
    snapshot_sha256: str | None = None

    def to_dict(self) -> dict[str, str]:
        row = {"sha256": self.sha256, "comparison": self.comparison}
        if self.snapshot_path is not None:
            row["snapshot_path"] = self.snapshot_path
        if self.snapshot_sha256 is not None:
            row["snapshot_sha256"] = self.snapshot_sha256
        return row


@dataclass(frozen=True)
class SourceInputs:
    """Versioned collection of named dossier inputs."""

    records: dict[str, SourceInputRecord]

    def to_dict(self) -> dict[str, Any]:
        rows = {label: self.records[label].to_dict() for label in sorted(self.records)}
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        return {
            "version": BLOCKED_PACKAGE_VERSION,
            "aggregate_sha256": hashlib.sha256(encoded).hexdigest(),
            "records": rows,
        }


@dataclass(frozen=True)
class FreshResult:
    """Verified package path or ordered reasons it cannot be used."""

    package_path: Path | None = None
    mismatches: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.package_path is not None and not self.mismatches


class _FreshnessError(RuntimeError):
    """Stable freshness mismatch raised while loading a stored dossier."""


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
    entry = tio.inspect_ticket(slug)
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
        authored_drift=bool(entry.get("authored_drift")),
        authored_drift_reason=str(entry.get("authored_drift_reason", "")),
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
    from booley.ticket_board.acceptance_ledger import submitted_report

    if submitted_report(ctx.log_dir) is None:
        candidates.pop("developer_report", None)
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


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _state_projection(raw: bytes) -> bytes:
    try:
        state = require_dict(json.loads(raw.decode("utf-8-sig")), field="booley state")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"state input is malformed: {exc}") from exc
    projected = dict(state)
    projected.pop("last_updated", None)
    timeline = require_list(projected.get("timeline", []), field="state timeline")
    projected["timeline"] = [
        row
        for row in timeline
        if not (
            (record := as_dict(row)) is not None
            and record.get("endpoint_kind") == "mcp_tool"
            and record.get("mcp_tool") == "triage_report"
        )
    ]
    return json.dumps(projected, sort_keys=True, separators=(",", ":")).encode()


def _evidence_record(label: str, raw: bytes) -> SourceInputRecord:
    if label == "state":
        return SourceInputRecord(_sha256(_state_projection(raw)), "semantic")
    if label == "run_log":
        return SourceInputRecord(_sha256(raw), "snapshot")
    return SourceInputRecord(_sha256(raw))


def _run_git(worktree: Path, label: str, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} collection timed out") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{label} collection failed: {detail or 'git exited nonzero'}")
    return result.stdout


def _worktree_records(ctx: BlockedContext) -> dict[str, SourceInputRecord]:
    identity = str(ctx.worktree) if ctx.worktree is not None else "unavailable"
    records = {"worktree/context": SourceInputRecord(_sha256(identity.encode()))}
    if ctx.worktree is None:
        return records
    operations = {
        "worktree/head": ("rev-parse", "HEAD"),
        "worktree/status": ("status", "--porcelain=v1", "-z", "--untracked-files=no"),
        "worktree/diff": ("diff", "--binary", "HEAD", "--"),
    }
    for label, args in operations.items():
        records[label] = SourceInputRecord(_sha256(_run_git(ctx.worktree, label, *args)))
    raw_paths = _run_git(
        ctx.worktree, "worktree/untracked", "ls-files", "--others", "--exclude-standard", "-z"
    )
    for raw_path in sorted(value for value in raw_paths.split(b"\0") if value):
        relative = raw_path.decode(errors="surrogateescape")
        path = ctx.worktree / relative
        try:
            if path.is_symlink():
                content = str(path.readlink()).encode(errors="surrogateescape")
            elif path.is_file():
                content = path.read_bytes()
            elif path.is_dir():
                content = b"nested-repository"
            else:
                raise OSError("input vanished during collection")
        except OSError as exc:
            raise RuntimeError(f"worktree/untracked/{relative} collection failed: {exc}") from exc
        records[f"worktree/untracked/{relative}"] = SourceInputRecord(_sha256(content))
    return records


def _evidence_bytes(ctx: BlockedContext, label: str, path: Path) -> bytes:
    from booley.ticket_board.acceptance_ledger import effective_state_bytes

    content = path.read_bytes()
    return effective_state_bytes(ctx.log_dir, content) if label == "state" else content


def _collect_live_inputs(ctx: BlockedContext) -> SourceInputs:
    records: dict[str, SourceInputRecord] = {}
    for label, path in _evidence_paths(ctx):
        try:
            records[label] = _evidence_record(label, _evidence_bytes(ctx, label, path))
        except OSError as exc:
            raise RuntimeError(f"{label} collection failed: {exc}") from exc
    records.update(_worktree_records(ctx))
    return SourceInputs(records)


def _snapshot_inputs(ctx: BlockedContext) -> tuple[SourceInputs, list[tuple[str, Path]]]:
    snapshot_root = ctx.runtime_dir / "evidence" / str(time.monotonic_ns())
    records: dict[str, SourceInputRecord] = {}
    prompt_paths: list[tuple[str, Path]] = []
    for label, path in _evidence_paths(ctx):
        raw = _evidence_bytes(ctx, label, path)
        snapshot = snapshot_root / label
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(raw)
        base = _evidence_record(label, raw)
        records[label] = SourceInputRecord(
            base.sha256,
            base.comparison,
            str(snapshot),
            _sha256(raw),
        )
        prompt_paths.append((label, snapshot))
    records.update(_worktree_records(ctx))
    inputs = SourceInputs(records)
    mismatches = _compare_inputs(inputs, _collect_live_inputs(ctx), include_snapshot=True)
    if mismatches:
        raise RuntimeError(
            f"blocked-ticket evidence changed while snapshotting: {', '.join(mismatches)}"
        )
    return inputs, prompt_paths


def _compare_inputs(
    expected: SourceInputs, current: SourceInputs, *, include_snapshot: bool = False
) -> tuple[str, ...]:
    labels = sorted(set(expected.records) | set(current.records))
    mismatches: list[str] = []
    for label in labels:
        old = expected.records.get(label)
        new = current.records.get(label)
        if old is not None and old.comparison == "snapshot" and not include_snapshot:
            continue
        if old is None:
            mismatches.append(f"{label} added")
        elif new is None:
            mismatches.append(f"{label} missing")
        elif old.sha256 != new.sha256:
            mismatches.append(f"{label} changed")
    return tuple(mismatches)


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


def _prompt(ctx: BlockedContext, evidence: list[tuple[str, Path]]) -> str:
    paths = "\n".join(f"- {label}: `{path}`" for label, path in evidence)
    worktree = str(ctx.worktree) if ctx.worktree else "not available"
    return f"""Diagnose blocked Booley ticket `{ctx.slug}` after its developer run.

Read the evidence below. Inspect the worktree with read-only Git commands when useful.
Distinguish current blockers from passing checks, stale-but-fixed findings, and warnings.
Report every independent blocker. Preserve the latest board transition reason exactly in
`board_reason`; call out conflicts in findings. Recommend only applicable choices grounded
in evidence: investigate, retry with feedback, relax a threshold, make a mandatory Criterion
optional, expand Scope, requested review, reset, fresh authoring, archive, or defer. Never
recommend removing Criteria. Explain the intended before/after change when recommending an
amendment, and do not modify files.

Worktree: `{worktree}`
Evidence:
{paths}
"""


async def _invoke(ctx: BlockedContext, evidence: list[tuple[str, Path]]) -> AgentResult:
    cfg = get_backend_config()
    return await call_agent(
        configure_agent_call(
            AgentCallParams(
                prompt=_prompt(ctx, evidence),
                system_prompt=(
                    "You are a read-only senior incident reviewer preparing a concise "
                    "blocked-ticket triage dossier grounded only in supplied evidence."
                ),
                model=cfg.settings.model_for_role("triage_report", "standard"),
                reasoning_effort=cfg.settings.effort_for_tier("standard"),
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


def _manifest_inputs(value: Any) -> SourceInputs:
    source = require_dict(value, field="blocked dossier source_inputs")
    if source.get("version") != BLOCKED_PACKAGE_VERSION:
        raise _FreshnessError("manifest version changed")
    rows = require_dict(source.get("records"), field="blocked dossier source records")
    records: dict[str, SourceInputRecord] = {}
    for raw_label, raw_record in rows.items():
        label = require_str_value(raw_label, field="blocked dossier source label")
        row = require_dict(raw_record, field=f"blocked dossier source {label}")
        comparison = require_str(row, "comparison")
        expected_comparison = (
            "semantic" if label == "state" else "snapshot" if label == "run_log" else "exact"
        )
        if comparison != expected_comparison:
            raise _FreshnessError(f"manifest source {label} has invalid comparison")
        records[label] = SourceInputRecord(
            require_str(row, "sha256"),
            comparison,
            require_opt_str(row, "snapshot_path"),
            require_opt_str(row, "snapshot_sha256"),
        )
    inputs = SourceInputs(records)
    if inputs.to_dict()["aggregate_sha256"] != source.get("aggregate_sha256"):
        raise _FreshnessError("manifest source aggregate changed")
    return inputs


def _snapshot_mismatches(inputs: SourceInputs) -> tuple[str, ...]:
    mismatches = []
    for label, record in sorted(inputs.records.items()):
        if record.snapshot_path is None:
            continue
        try:
            digest = _sha256(Path(record.snapshot_path).read_bytes())
        except OSError:
            mismatches.append(f"{label} evidence missing")
            continue
        if digest != record.snapshot_sha256:
            mismatches.append(f"{label} evidence integrity changed")
    return tuple(mismatches)


def _load_manifest(ctx: BlockedContext) -> dict[str, Any]:
    try:
        manifest = require_dict(
            json.loads(_manifest_path(ctx).read_text(encoding="utf-8")),
            field="blocked dossier manifest",
        )
    except FileNotFoundError as exc:
        raise _FreshnessError("manifest missing") from exc
    except (OSError, ValueError) as exc:
        raise _FreshnessError("manifest malformed") from exc
    if manifest.get("version") != BLOCKED_PACKAGE_VERSION:
        raise _FreshnessError("manifest version changed")
    if manifest.get("status") != "ready":
        error = manifest.get("error")
        detail = f"manifest failed: {error}" if isinstance(error, str) else "manifest not ready"
        raise _FreshnessError(detail)
    return manifest


def _manifest_package(manifest: dict[str, Any]) -> tuple[SourceInputs, Path]:
    try:
        inputs = _manifest_inputs(manifest.get("source_inputs"))
        package_path = Path(require_str(manifest, "package_path"))
        package_hash = _sha256(package_path.read_bytes())
    except FileNotFoundError as exc:
        raise _FreshnessError("package missing") from exc
    except (OSError, ValueError) as exc:
        raise _FreshnessError("manifest malformed") from exc
    if package_hash != manifest.get("package_sha256"):
        raise _FreshnessError("package integrity changed")
    return inputs, package_path


def _fresh(ctx: BlockedContext) -> FreshResult:
    try:
        manifest = _load_manifest(ctx)
        inputs, package_path = _manifest_package(manifest)
    except _FreshnessError as exc:
        return FreshResult(mismatches=(str(exc),))
    mismatches = list(_snapshot_mismatches(inputs))
    try:
        mismatches.extend(_compare_inputs(inputs, _collect_live_inputs(ctx)))
    except (OSError, RuntimeError, ValueError) as exc:
        mismatches.append(str(exc))
    return FreshResult(package_path if not mismatches else None, tuple(mismatches))


def _publish_blocked_dossier(
    ctx: BlockedContext,
    slug: str,
    diagnosis: dict,
    result: Any,
    duration: float,
    source_inputs: SourceInputs,
) -> Path:
    package = {
        "version": BLOCKED_PACKAGE_VERSION,
        "kind": "blocked",
        "slug": slug,
        "ticket_path": str(ctx.ticket_path),
        "blocked_log_path": str(ctx.log_dir / "blocked.md"),
        "authored_drift": ctx.authored_drift,
        "authored_drift_reason": ctx.authored_drift_reason,
        "diagnosis": diagnosis,
    }
    path = _package_path(ctx)
    _write_json(path, package)
    _write_json(
        _manifest_path(ctx),
        {
            "version": BLOCKED_PACKAGE_VERSION,
            "status": "ready",
            "source_inputs": source_inputs.to_dict(),
            "package_path": str(path),
            "package_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "duration_s": round(duration, 2),
            "cost_usd": round(result.cost_usd, 4),
            "updated_at": utc_now_rfc3339(),
        },
    )
    return path


async def prepare_blocked_dossier(
    project_root: Path, slug: str, *, force: bool = False
) -> BlockedPrepOutcome:
    """Prepare a best-effort dossier after a ticket remains blocked."""
    started = time.monotonic()
    try:
        ctx = _resolve_context(project_root.resolve(), slug)
        if not force:
            fresh = _fresh(ctx)
            if fresh.ready:
                return BlockedPrepOutcome(
                    "fresh", "blocked dossier is current", fresh.package_path
                )
        source_inputs, evidence = _snapshot_inputs(ctx)
        result = await _invoke(ctx, evidence)
        diagnosis = _validate(result.structured)
        mismatches = _compare_inputs(source_inputs, _collect_live_inputs(ctx))
        if mismatches:
            raise RuntimeError(
                f"blocked-ticket evidence changed during diagnosis: {', '.join(mismatches)}"
            )
        duration = time.monotonic() - started
        _record_call(ctx, result, duration)
        mismatches = _compare_inputs(source_inputs, _collect_live_inputs(ctx))
        if mismatches:
            raise RuntimeError(
                f"blocked-ticket evidence changed during accounting: {', '.join(mismatches)}"
            )
        path = _publish_blocked_dossier(ctx, slug, diagnosis, result, duration, source_inputs)
        return BlockedPrepOutcome("ready", "blocked dossier prepared", path)
    except Exception as exc:
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
        fresh = _fresh(ctx)
        if not fresh.ready:
            detail = ", ".join(fresh.mismatches)
            return BlockedPrepOutcome("stale", f"blocked dossier is stale: {detail}")
        path = fresh.package_path
        assert path is not None
        package = json.loads(path.read_text(encoding="utf-8"))
        diagnosis = _validate(package.get("diagnosis"))
        lines = [f"### {slug}", "", "**Blocked by:**", ""]
        if package.get("authored_drift"):
            lines.extend(
                [
                    f"**Authored drift:** {package.get('authored_drift_reason', '')}; "
                    "use return-to-draft",
                    "",
                ]
            )
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
    except Exception as exc:  # noqa: BLE001 — CLI boundary returns a stable outcome
        return BlockedPrepOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])
