"""Test-only helpers that store or remove EDA grants without Runtime coordination."""

from __future__ import annotations

from pathlib import Path

from booley.eda.provisioning import authority


def add_grant(
    project: Path,
    kind: str = authority.VIVADO_KIND,
    *,
    installation: str | None = None,
    license_profile: str | None = None,
) -> authority.ProjectGrant:
    """Authorize one Project directly in EDA authority state."""
    with authority._prepare_add_grant(
        authority.grant_project_identity(project),
        kind,
        installation=installation,
        license_profile=license_profile,
    ) as commit:
        return commit()


def revoke_grant(project: Path, kind: str = authority.VIVADO_KIND) -> authority.ProjectGrant:
    """Remove one Project grant directly from EDA authority state."""
    with authority._prepare_revoke_grant(
        authority.revoke_project_identity(project, kind), kind
    ) as commit:
        return commit()


def register_license(name: str = "registered") -> authority.LicenseProfile:
    """Register a License Profile with fixed documentation-range addresses."""
    return authority.register_license(
        name,
        server_ipv4="192.0.2.1",
        server_hostid="license-host",
        lmgrd_port=27000,
        vendor_port=27001,
    )
