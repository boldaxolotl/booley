"""Stage review approval writes and recover only their exact before/after bytes.

The immutable inspection binds the original inputs. A transaction is durable
before any live file changes, and retry checks all other review inputs against
that inspection. Unfinished transactions still require explicit Human answers;
neither a promotion plan nor a transaction supplies approval authority.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_list, require_str
from booley.flows.sim.coverage_waiver_application import WaiverPromotionPlan

from . import review_preparation as prep
from .board_layout import waiver_candidates_path
from .helpers import tickets_dir_from_project_root
from .persistence import atomic_replace_bytes, atomic_write_once
from .provisional_coverage import TicketCoverageContext
from .review_records import ReviewEntryError, digest, read_json
from .waiver_approval import (
    WaiverDecisionError,
    WaiverDecisions,
    _ApprovalInputs,
    _validate_resumed_plan,
    promotion_plan_path,
)


@dataclass(frozen=True)
class _Patch:
    label: str
    before: bytes | None
    after: bytes

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "before": _encoded(self.before),
            "after": _encoded(self.after),
        }


def _encoded(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode("ascii") if content is not None else None


def _answers(decisions: WaiverDecisions) -> dict[str, Any]:
    return {
        "accepted": sorted(decisions.accepted),
        "rejected": sorted(decisions.rejected),
        "approval_ref": decisions.approval_ref,
    }


def _transaction_path(ctx: prep.ReviewPrepContext) -> Path:
    assert ctx.inspection is not None
    return ctx.log_dir / "review" / "waiver-decisions" / f"{ctx.inspection['generation']}.json"


def _candidate_path(ctx: prep.ReviewPrepContext) -> Path:
    return waiver_candidates_path(tickets_dir_from_project_root(ctx.project_root), ctx.slug)


def _path(ctx: prep.ReviewPrepContext, label: str) -> Path:
    if label == "waiver-candidates":
        path = _candidate_path(ctx)
    else:
        relative = Path(label)
        allowed = label in {".runtime/booley_state.json", "acceptance/waiver-promotion.json"}
        allowed |= relative.parts[:2] == ("acceptance", "evidence")
        if not allowed or relative.is_absolute() or ".." in relative.parts:
            raise ReviewEntryError("waiver approval transaction names an invalid path")
        path = ctx.log_dir / relative
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ReviewEntryError("waiver approval transaction names a symlink")
    return path


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _overrides(patches: list[_Patch], *, before: bool) -> dict[str, bytes | None]:
    return {
        patch.label: patch.before if before else patch.after
        for patch in patches
        if patch.label in {".runtime/booley_state.json", "waiver-candidates"}
        or patch.label.endswith("/record.json")
    }


def _parse_patches(record: Mapping[str, Any]) -> list[_Patch]:
    result = []
    for item in require_list(record.get("patches"), field="approval patches"):
        row = require_dict(item, field="approval patch")
        if set(row) != {"label", "before", "after"}:
            raise ReviewEntryError("waiver approval patch has invalid fields")
        before = row.get("before")
        result.append(
            _Patch(
                require_str(row, "label"),
                base64.b64decode(before, validate=True) if before is not None else None,
                base64.b64decode(require_str(row, "after"), validate=True),
            )
        )
    if len({patch.label for patch in result}) != len(result):
        raise ReviewEntryError("waiver approval transaction repeats a path")
    return result


def _validated_patches(ctx: prep.ReviewPrepContext, record: Mapping[str, Any]) -> list[_Patch]:
    assert ctx.inspection is not None
    if record.get("schema") != 1 or record.get("inspection_sha") != digest(ctx.inspection):
        raise ReviewEntryError("waiver approval transaction names another inspection")
    patches = _parse_patches(record)
    for patch in patches:
        if _read(_path(ctx, patch.label)) not in (patch.before, patch.after):
            raise ReviewEntryError("selected review inputs changed outside waiver approval")
    original = prep._source_fingerprint(ctx, source_overrides=_overrides(patches, before=True))
    if original != ctx.inspection["capture_sha"]:
        raise ReviewEntryError("selected review inputs changed outside waiver approval")
    _validate_plan(ctx, record, patches)
    return patches


def _validate_plan(
    ctx: prep.ReviewPrepContext, record: Mapping[str, Any], patches: list[_Patch]
) -> None:
    """A recovered plan is checked against the original candidates and current RTL."""
    plan_patch = next((p for p in patches if p.label == "acceptance/waiver-promotion.json"), None)
    if plan_patch is None:
        return
    plan = WaiverPromotionPlan.from_json(json.loads(plan_patch.after))
    answers = require_dict(record.get("answers"), field="waiver answers")
    decisions = WaiverDecisions(
        frozenset(require_list(answers.get("accepted"), field="accepted candidates")),
        frozenset(require_list(answers.get("rejected"), field="rejected candidates")),
        answers.get("approval_ref"),
    )
    assert ctx.inspection is not None
    with tempfile.TemporaryDirectory(prefix="booley-waiver-recovery-") as directory:
        tickets_dir = Path(directory)
        candidate = next((p for p in patches if p.label == "waiver-candidates"), None)
        original = candidate.before if candidate is not None else _read(_candidate_path(ctx))
        if original is not None:
            path = waiver_candidates_path(tickets_dir, ctx.slug)
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
        inputs = _ApprovalInputs(
            ctx.slug,
            ctx.log_dir,
            ctx.log_dir / ".runtime" / "booley_state.json",
            ctx.worktree,
            ctx.project_root,
            TicketCoverageContext(ctx.slug, tickets_dir, ctx.log_dir, ctx.worktree),
            ctx.inspection,
        )
        _validate_resumed_plan(inputs, plan, decisions)


def retry_capture(ctx: prep.ReviewPrepContext) -> str | None:
    """Validate partial writes before allowing selected-package freshness checks."""
    record = read_json(_transaction_path(ctx))
    if record is None:
        return None
    _validated_patches(ctx, record)
    return prep._source_fingerprint(ctx)


def _copy_inputs(ctx: prep.ReviewPrepContext, staging: Path) -> None:
    state = ctx.log_dir / ".runtime" / "booley_state.json"
    destination = staging / "log" / ".runtime" / "booley_state.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(state.read_bytes())
    candidate = _candidate_path(ctx)
    if candidate.is_file():
        target = waiver_candidates_path(staging / "tickets", ctx.slug)
        target.parent.mkdir(parents=True)
        target.write_bytes(candidate.read_bytes())
    evidence = ctx.log_dir / "acceptance" / "evidence"
    if evidence.is_dir():
        shutil.copytree(evidence, staging / "log" / "acceptance" / "evidence")


def _staged_patches(ctx: prep.ReviewPrepContext, staging: Path) -> list[_Patch]:
    outputs = {
        path.relative_to(staging / "log").as_posix(): path
        for path in sorted((staging / "log").rglob("*"))
        if path.is_file()
    }
    candidate = waiver_candidates_path(staging / "tickets", ctx.slug)
    if candidate.is_file():
        outputs["waiver-candidates"] = candidate
    return [
        _Patch(label, before, after)
        for label, path in outputs.items()
        if (after := path.read_bytes()) != (before := _read(_path(ctx, label)))
    ]


def _stage(
    ctx: prep.ReviewPrepContext,
    decisions: WaiverDecisions,
    apply: Callable[[Path], object],
) -> dict[str, Any] | None:
    if promotion_plan_path(ctx.log_dir).exists():
        raise WaiverDecisionError(
            "promotion plan has no bound approval transaction; use board review"
        )
    error = ""
    with tempfile.TemporaryDirectory(prefix="booley-waiver-decision-") as directory:
        staging = Path(directory)
        _copy_inputs(ctx, staging)
        try:
            apply(staging)
        except WaiverDecisionError as exc:
            error = str(exc)
        patches = _staged_patches(ctx, staging)
    if not patches:
        if error:
            raise WaiverDecisionError(error)
        return None
    assert ctx.inspection is not None
    return {
        "schema": 1,
        "inspection_sha": digest(ctx.inspection),
        "answers": _answers(decisions),
        "patches": [patch.to_json() for patch in patches],
        "error": error,
    }


def apply_transaction(
    ctx: prep.ReviewPrepContext,
    decisions: WaiverDecisions,
    apply: Callable[[Path], object],
) -> str | None:
    """Stage once, then idempotently publish exact files; require answers on retries."""
    path = _transaction_path(ctx)
    record = read_json(path)
    if record is None:
        record = _stage(ctx, decisions, apply)
        if record is None:
            return None
        _validated_patches(ctx, record)
        atomic_write_once(path, (json.dumps(record, sort_keys=True) + "\n").encode())
    if record.get("answers") != _answers(decisions):
        raise WaiverDecisionError(
            "repeat the explicit waiver decisions to retry unfinished approval"
        )
    patches = _validated_patches(ctx, record)
    for patch in patches:
        destination = _path(ctx, patch.label)
        if _read(destination) != patch.after:
            atomic_replace_bytes(destination, patch.after)
    return prep._source_fingerprint(ctx)


def raise_transaction_error(ctx: prep.ReviewPrepContext) -> None:
    """Report prediction failure after its rejections and recovery capture are durable."""
    record = read_json(_transaction_path(ctx))
    if record is not None and record.get("error"):
        raise WaiverDecisionError(require_str(record, "error"))
