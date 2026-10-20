"""Outer-layer coordination for EDA grant and Sandbox mutations."""

from __future__ import annotations

from pathlib import Path

from booley.eda.provisioning import authority
from booley.runtime import issuance_invalidation, lifecycle_lock, session_refresh


class GrantCoordinator:
    """Serialize grant changes with durable Runtime invalidation."""

    def recovery_pending(self) -> bool:
        try:
            return session_refresh.shared_recovery_blocks_command(read_only=True)
        except RuntimeError as exc:
            raise authority.AuthorityError(str(exc)) from exc

    def add(
        self,
        project: Path,
        kind: str,
        *,
        installation: str | None,
        license_profile: str | None,
    ) -> authority.ProjectGrant:
        """Withdraw the Sandbox stamp, then store the grant; no journal is needed.

        Invalidation happens before the authority state is written, so a crash
        leaves the stamp withdrawn and the grant absent, which is a safe state.
        """
        try:
            with lifecycle_lock.host_lifecycle_lock("EDA grant add"):
                session_refresh.shared_recovery_blocks_command(read_only=False)
                identity = authority.grant_project_identity(project)
                with authority._prepare_add_grant(
                    identity,
                    kind,
                    installation=installation,
                    license_profile=license_profile,
                ) as commit:
                    issuance_invalidation.invalidate(identity)
                    return commit()
        except issuance_invalidation.InvalidationError as exc:
            raise authority.AuthorityError(str(exc)) from exc

    def revoke(self, project: Path, kind: str) -> authority.ProjectGrant:
        """Journal, persist the revocation, then withdraw the stamp and clean up."""
        try:
            with lifecycle_lock.host_lifecycle_lock("EDA grant revoke"):
                session_refresh.shared_recovery_blocks_command(read_only=False)
                identity = authority.revoke_project_identity(project, kind)
                with authority._prepare_revoke_grant(identity, kind) as commit:
                    issuance_invalidation.prepare(identity)
                    revoked = commit()
                issuance_invalidation.recover_project_locked(identity)
                return revoked
        except issuance_invalidation.InvalidationError as exc:
            raise authority.AuthorityError(str(exc)) from exc


COORDINATOR = GrantCoordinator()
