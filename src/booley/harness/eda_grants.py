"""Outer-layer coordination for EDA grant and Session Runtime mutations."""

from __future__ import annotations

from pathlib import Path

from booley.eda.provisioning import authority
from booley.eda.provisioning.licensing.flexnet_docker import (
    cleanup_project_resources_for_identity,
)
from booley.runtime import issuance_invalidation


class GrantCoordinator:
    """Serialize grant changes with durable Runtime invalidation."""

    def add(
        self,
        project: Path,
        kind: str,
        *,
        installation: str | None,
        license_profile: str | None,
    ) -> authority.ProjectGrant:
        try:
            return issuance_invalidation.coordinate_mutation(
                operation="EDA grant add",
                resolve_project_identity=lambda: authority.grant_project_identity(project),
                mutation=lambda identity: authority._prepare_add_grant(
                    identity,
                    kind,
                    installation=installation,
                    license_profile=license_profile,
                ),
                cleanup_resources=cleanup_project_resources_for_identity,
                invalidate_before_mutation=True,
                cleanup_after_mutation=False,
            )
        except issuance_invalidation.InvalidationError as exc:
            raise authority.AuthorityError(str(exc)) from exc

    def revoke(self, project: Path, kind: str) -> authority.ProjectGrant:
        try:
            return issuance_invalidation.coordinate_mutation(
                operation="EDA grant revoke",
                resolve_project_identity=lambda: authority.revoke_project_identity(project, kind),
                mutation=lambda identity: authority._prepare_revoke_grant(identity, kind),
                cleanup_resources=cleanup_project_resources_for_identity,
                invalidate_before_mutation=False,
                cleanup_after_mutation=True,
            )
        except issuance_invalidation.InvalidationError as exc:
            raise authority.AuthorityError(str(exc)) from exc


COORDINATOR = GrantCoordinator()
