"""Host EDA CLI ordering and Docker-cleanup boundary tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from booley.eda import authority, cli


def test_revoke_removes_authority_before_containers_and_networks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = authority.ProjectGrant(str(project), "vivado", "vivado_2025_2", "site")
    args = argparse.Namespace(
        eda_group="grant", eda_action="revoke", project=project, kind="vivado"
    )
    events: list[str] = []
    with (
        patch.object(
            authority,
            "revoke_grant",
            side_effect=lambda *_args: (events.append("authority"), grant)[1],
        ),
        patch(
            "booley.eda.flexnet_docker.cleanup_project_resources",
            side_effect=lambda _project: (events.append("cleanup"), ())[1],
        ),
    ):
        result = cli._grant_action(args, "revoke")

    assert events == ["authority", "cleanup"]
    assert result["residual_resources"] == []


def test_revoke_reports_residue_without_restoring_authority(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = authority.ProjectGrant(str(project), "vivado")
    args = argparse.Namespace(
        eda_group="grant", eda_action="revoke", project=project, kind="vivado"
    )

    with (
        patch.object(authority, "revoke_grant", return_value=grant) as revoke,
        patch.object(cli, "_cleanup_project_resources", return_value=["network:private"]),
    ):
        assert cli.run(args, project) == 2

    revoke.assert_called_once()
    assert "grant revoked" in capsys.readouterr().err
