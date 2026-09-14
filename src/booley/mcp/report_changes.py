"""Validate final-report explanations against committed Ticket changes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from booley.runtime.git import git_run
from booley.ticket_board.acceptance_basis import AcceptanceBasis
from booley.ticket_board.ticket_document import (
    convert_ticket_document,
    ticket_conversion_context,
)
from booley.ticket_board.ticket_repositories import TicketWorkspaceError, ticket_repositories


def changed_ticket_paths(worktree: Path) -> list[str]:
    """Read net changed paths in every repository against the pinned authoring base.

    Rename detection is disabled so both the removed and added paths require an
    explanation. Without a Ticket, use the branch's configured upstream.
    """
    ticket_file = os.environ.get("BOOLEY_TICKET_FILE", "")
    basis = None
    if ticket_file:
        path = Path(ticket_file)
        with ticket_conversion_context(worktree, path.stem, "executable") as context:
            converted = convert_ticket_document(path.read_text(encoding="utf-8"), context)
        if converted.document is None:
            detail = "; ".join(item.message for item in converted.diagnostics)
            raise TicketWorkspaceError(f"Ticket document is invalid: {detail}")
        basis = AcceptanceBasis.from_mapping(converted.document.generated.get("acceptance_basis"))
    paths: set[str] = set()
    repositories = ticket_repositories(
        worktree, require_paired=os.environ.get("BOOLEY_PAIRED_PROJECT_REPOSITORY") == "1"
    )
    for repository in repositories:
        base = "@{upstream}"
        if basis is not None:
            base = basis.project_sha if repository.path_prefix else basis.outer_sha
            if not base:
                raise TicketWorkspaceError("No Acceptance Basis for paired repository")
        result = git_run(
            repository.worktree,
            ["diff", "--no-renames", "--name-only", "-z", base, "HEAD"],
            timeout=30,
        )
        if result.returncode:
            raise TicketWorkspaceError(
                f"Cannot determine changed files in {repository.worktree}: {result.stderr.strip()}"
            )
        paths.update(repository.prefixed_path(path) for path in result.stdout.split("\0") if path)
    return sorted(paths)


def validate_justifications(raw: str | None, paths: list[str]) -> dict[str, str]:
    """Require exactly one nonblank explanation per changed path."""
    if raw is None:
        raise ValueError("--file-justifications is required (JSON object of path: explanation)")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("file-justifications must be a JSON object")
    invalid = [
        path for path, reason in value.items() if not isinstance(reason, str) or not reason.strip()
    ]
    missing = sorted(set(paths) - value.keys())
    extra = sorted(value.keys() - set(paths))
    if missing or extra or invalid:
        raise ValueError(
            f"File justifications do not match changed files. Missing: {missing}; "
            f"unknown paths: {extra}; blank or non-text explanations: {invalid}"
        )
    return {path: value[path].strip() for path in paths}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate file justification: {key}")
        result[key] = value
    return result
