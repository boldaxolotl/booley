"""Live Ticket Workspace validation against immutable generated inputs."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from booley.fusesoc.core_projection import (
    CoreProjectionError,
    native_cores_ignored,
    reconcile_isolated_registry,
    reconcile_projected_cores,
)
from booley.runtime.project_dir import (
    checkout_project_dir_relative_to,
    resolve_checkout_project_dir,
)

from .acceptance_basis import (
    BLOCK_REASON,
    AcceptanceBasis,
    AcceptanceBasisError,
    assert_candidate_inputs_unchanged,
    materialize_basis_checkout,
)


def assert_ticket_worktree_inputs_unchanged(
    project_root: Path | str,
    basis: AcceptanceBasis,
    live_checkout: Path | str,
) -> None:
    """Validate one live Ticket Workspace against a rendered immutable Basis."""
    try:
        _validate_live_worktree(Path(project_root), basis, Path(live_checkout))
    except (AcceptanceBasisError, CoreProjectionError, OSError, ValueError) as exc:
        raise _canonical_block_error(exc) from exc


def _validate_live_worktree(
    project_root: Path,
    basis: AcceptanceBasis,
    live_checkout: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="booley-live-basis-") as raw_directory:
        reference = materialize_basis_checkout(
            project_root,
            basis,
            Path(raw_directory) / "checkout",
        )
        _require_contained_project_directory(reference)
        reconcile_projected_cores(reference)
        if native_cores_ignored(reference):
            reconcile_isolated_registry(reference)
        _assert_renderers_preserved_authoring(reference, basis)
        assert_candidate_inputs_unchanged(
            basis,
            project_root,
            live_checkout,
            reference,
        )


def _require_contained_project_directory(reference: Path) -> None:
    project_dir = resolve_checkout_project_dir(reference).resolve()
    try:
        project_dir.relative_to(reference.resolve())
    except ValueError as exc:
        raise AcceptanceBasisError(
            f"materialized project directory {project_dir} is outside {reference}"
        ) from exc


def _assert_renderers_preserved_authoring(
    reference: Path,
    basis: AcceptanceBasis,
) -> None:
    repositories = [(reference, basis.participant("outer"))]
    project = next((item for item in basis.participants if item.role == "project"), None)
    if project is not None:
        relative = checkout_project_dir_relative_to(reference)
        repositories.append((reference / relative, project))
    for repository, participant in repositories:
        changed = _tracked_paths_since(repository, participant.authoring_sha)
        if changed:
            raise AcceptanceBasisError(
                "generated-input renderer changed tracked "
                f"{participant.role} path(s): {', '.join(sorted(changed))}"
            )


def _tracked_paths_since(repository: Path, authoring_sha: str) -> set[str]:
    commands = (
        ("diff", "--name-only", "-z", authoring_sha),
        ("diff", "--cached", "--name-only", "-z", authoring_sha),
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(
            ["git", *command],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise AcceptanceBasisError(f"git {' '.join(command)} failed in {repository}: {detail}")
        paths.update(path for path in result.stdout.split("\0") if path)
    return paths


def _canonical_block_error(exc: Exception) -> AcceptanceBasisError:
    marker = f"{BLOCK_REASON}:"
    detail = str(exc).strip()
    while detail.startswith(marker):
        detail = detail.removeprefix(marker).lstrip()
    return AcceptanceBasisError(f"{marker} {detail}".rstrip())
