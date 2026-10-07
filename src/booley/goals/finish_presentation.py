"""Versioned immutable presentation bytes, independent of future renderer releases."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from booley.core.boundary import require_dict, require_str_value
from booley.goals.lifecycle import LifecycleOperation
from booley.goals.proposals import encode
from booley.review.goal_package import GoalCompletionPackage, render_goal_html
from booley.review.goal_presentation_v1 import render_goal_html as legacy_html


def response(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
    """Paths belong to this operation; message bytes are the literal frozen summary."""
    return {
        "status": "finished",
        "record_id": operation.request.record_id,
        "operation_id": operation.request.operation_id,
        "package": str(operation.directory / "review-package.json"),
        "summary": str(operation.directory / "SUMMARY.md"),
        "history_path": frozen["path"] if frozen["skip_publication"] is None else None,
        "publication_note": frozen["skip_publication"],
        "running_jobs": frozen["running_jobs"],
        "html": str(operation.directory / "explanation.html")
        if operation.request.explain_html
        else None,
        "message": bytes.fromhex(frozen["summary_hex"]).decode("utf-8"),
    }


def freeze_presentation(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    """Freeze requested HTML and the complete saved response before the publication fence."""
    html = render_goal_html(GoalCompletionPackage.from_json(frozen["package"])).encode("utf-8")
    frozen["presentation"] = {
        "recipe": "booley.goal-presentation/v2",
        "html_hex": html.hex() if operation.request.explain_html else None,
        "html_digest": hashlib.sha256(html).hexdigest()
        if operation.request.explain_html
        else None,
        "response_hex": encode(response(operation, frozen)).hex(),
        "response_digest": hashlib.sha256(encode(response(operation, frozen))).hexdigest(),
    }


def html_bytes(frozen: dict[str, Any]) -> bytes:
    """Legacy attempts use their frozen recipe, never the current renderer."""
    if frozen["schema"] == "booley.goal-finish-attempt/v1":
        return legacy_html(GoalCompletionPackage.from_json(frozen["package"])).encode("utf-8")
    return bytes.fromhex(frozen["presentation"]["html_hex"])


def frozen_response(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
    if frozen["schema"] == "booley.goal-finish-attempt/v1":
        return response(operation, frozen)
    return require_dict(
        json.loads(bytes.fromhex(frozen["presentation"]["response_hex"])), field="frozen response"
    )


def validate_presentation(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    presentation = require_dict(frozen["presentation"], field="frozen presentation")
    if (
        set(presentation)
        != {"recipe", "html_hex", "html_digest", "response_hex", "response_digest"}
        or presentation["recipe"] != "booley.goal-presentation/v2"
    ):
        raise ValueError("invalid frozen presentation schema")
    for name in ("html", "response"):
        value = presentation[name + "_hex"]
        if value is None and name == "html" and not operation.request.explain_html:
            if presentation["html_digest"] is not None:
                raise ValueError("unrequested HTML has a digest")
            continue
        content = bytes.fromhex(require_str_value(value, field=name + " frozen bytes"))
        if hashlib.sha256(content).hexdigest() != presentation[name + "_digest"]:
            raise ValueError("frozen presentation digest differs")
    saved = frozen_response(operation, frozen)
    expected = response(operation, frozen)
    # Storage relocation changes the containing prefix, never the logical artifacts.
    for name, leaf in (
        ("summary", "SUMMARY.md"),
        ("package", "review-package.json"),
        ("html", "explanation.html"),
    ):
        if saved.get(name) is not None:
            if saved[name] != str(operation.sealed_directory("attempt") / leaf):
                raise ValueError("frozen response path differs from its logical operation")
            expected[name] = saved[name]
    if saved != expected:
        raise ValueError("frozen response differs from the complete completion identity")
