"""Validate the complete persisted finish boundary before any local or Git effect."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from booley.core.boundary import require_dict, require_int, require_list, require_str_value
from booley.goals.checkout import branch_ref
from booley.goals.completion_capture import validate_capture
from booley.goals.finish_presentation import html_bytes, validate_presentation
from booley.goals.input_identity import parse_bindings, require_bindings
from booley.goals.lifecycle import LifecycleError, LifecycleOperation
from booley.goals.model import GoalRecord
from booley.goals.proposals import digest, encode
from booley.review.goal_package import (
    GoalCompletionPackage,
)
from booley.review.goal_presentation_v1 import render_goal_briefing as legacy_briefing

_ATTEMPT_FIELDS = set(
    {
        "schema",
        "operation_id",
        "record_revision",
        "authority_digest",
        "inputs",
        "package",
        "package_digest",
        "summary_hex",
        "summary_digest",
        "path",
        "destination_before",
        "mode",
        "skip_publication",
        "worktree",
        "branch",
        "running_jobs",
    }
)


def validate_attempt(frozen: dict[str, Any], operation: LifecycleOperation) -> None:
    """Independent authority, durable fence and request all bind the same full attempt."""
    try:
        record = operation.store.load(operation.request.record_id)
        if record.finish_operation == operation.request.operation_id and (
            record.package_digest != "sha256:" + frozen["package_digest"]
            or record.finish_attempt_digest != "sha256:" + digest(frozen)
        ):
            raise ValueError("attempt differs from the durable finishing fence")
        version = frozen.get("schema")
        fields = (
            _ATTEMPT_FIELDS
            if version == "booley.goal-finish-attempt/v1"
            else _ATTEMPT_FIELDS | {"private_capture", "presentation"}
        )
        if set(frozen) != fields or version not in {
            "booley.goal-finish-attempt/v1",
            "booley.goal-finish-attempt/v2",
        }:
            raise ValueError("unknown finish attempt schema/fields")
        if (
            frozen["operation_id"] != operation.request.operation_id
            or frozen["worktree"] != record.worktree.to_json()
            or frozen["branch"] != branch_ref(record.branch)
        ):
            raise ValueError("attempt identity differs from the bound record/request")
        if require_int(frozen["record_revision"], field="record_revision") < 0:
            raise ValueError("attempt revision must be nonnegative")
        if frozen["destination_before"] != "absent" or frozen["mode"] != "100644":
            raise ValueError("invalid publication destination/mode")
        _validate_path(frozen["path"], record.id, operation.request.operation_id)
        _validate_package(frozen, operation, record)
        if version == "booley.goal-finish-attempt/v2":
            validate_capture(frozen["private_capture"], frozen["package"]["input_proof"])
            validate_presentation(operation, frozen)
        skip = frozen["skip_publication"]
        if skip not in {
            None,
            "summary kept local; Stealth publication is skipped",
            "summary kept local; `.booley_project` is git-excluded",
        }:
            raise ValueError("invalid publication skip identity")
        for row in require_list(frozen["running_jobs"], field="running_jobs"):
            require_dict(row, field="running Job")
    except (ValueError, TypeError, KeyError) as exc:
        raise LifecycleError(f"invalid frozen finish attempt: {exc}") from exc


def _validate_path(raw: object, record_id: str, operation_id: str) -> None:
    path = require_str_value(raw, field="publication path")
    allowed = {
        f".booley_project/goals/history/{record_id}.md",
        f".booley_project/goals/history/{record_id}-{operation_id}.md",
    }
    if (
        path not in allowed
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).is_absolute()
    ):
        raise ValueError("publication path differs from its literal confined operation path")


def _validate_package(
    frozen: dict[str, Any], operation: LifecycleOperation, record: GoalRecord
) -> None:
    package = GoalCompletionPackage.from_json(frozen["package"])
    facts = package.to_json()
    original = GoalRecord.from_json(facts["record"])
    if (
        original.id != record.id
        or original.worktree != record.worktree
        or original.branch != record.branch
        or facts["session_summary"] != operation.request.summary
    ):
        raise ValueError("package differs from the original bound record/request")
    _validate_inputs(frozen, record, facts)
    if frozen["package_digest"] != digest(facts):
        raise ValueError("package digest differs from the frozen package")
    content = bytes.fromhex(require_str_value(frozen["summary_hex"], field="summary_hex"))
    if (
        frozen["schema"] == "booley.goal-finish-attempt/v1"
        and content != legacy_briefing(package).encode("utf-8")
    ) or frozen["summary_digest"] != hashlib.sha256(content).hexdigest():
        raise ValueError("summary differs from the frozen package")
    for name in ("authority_digest", "package_digest", "summary_digest"):
        text = require_str_value(frozen[name], field=name)
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"{name} must be a canonical SHA-256 digest")


def _validate_inputs(frozen: dict[str, Any], record: GoalRecord, facts: dict[str, Any]) -> None:
    inputs = require_dict(frozen["inputs"], field="inputs")
    if set(inputs) != {
        "rtl",
        "rtl_pin",
        "rtl_base",
        "project",
        "project_repository",
        "project_pin",
        "project_base",
        "topology",
        "roots",
    }:
        raise ValueError("invalid participant input schema")
    roots = parse_bindings(inputs["roots"])
    if record.input_paths is not None:
        require_bindings({key: record.input_paths[key] for key in roots}, roots)
    if (
        record.input_topology_digest is not None
        and record.input_topology_digest != "sha256:" + inputs["topology"]
    ):
        raise ValueError("frozen participant topology differs from entry authority")
    for key in ("rtl", "project", "project_repository"):
        role = "paired" if key == "project_repository" else key
        if inputs[key] != (None if role not in roots else roots[role]["path"]):
            raise ValueError("participant spelling differs from proven input roots")
    if (
        inputs["rtl_base"] != record.base_sha
        or inputs["project_base"] != record.paired_project_base_sha
        or facts["base_sha"] != inputs["rtl_base"]
        or facts["head_sha"] != inputs["rtl_pin"]
        or facts["branch"] != frozen["branch"].removeprefix("refs/heads/")
    ):
        raise ValueError("package participant pins/bases/branch differ from the attempt")
    proof = require_dict(facts["input_proof"], field="input_proof")
    if any(proof.get(key) != value for key, value in inputs.items()):
        raise ValueError("package input proof differs from the attempt")


def validate_local_artifacts(
    frozen: dict[str, Any], operation: LifecycleOperation, *, required: bool = False
) -> None:
    """Existing package/summary/HTML must match before another artifact can be written."""
    expected = {
        "SUMMARY.md": bytes.fromhex(frozen["summary_hex"]),
        "review-package.json": encode(frozen["package"]),
    }
    if operation.request.explain_html:
        expected["explanation.html"] = html_bytes(frozen)
    for name, content in expected.items():
        path = operation.directory / name
        if (
            path.is_symlink()
            or (path.exists() and (not path.is_file() or path.read_bytes() != content))
            or (required and not path.is_file())
        ):
            raise LifecycleError(
                f"persisted completion artifact differs from frozen authority: {path}"
            )
