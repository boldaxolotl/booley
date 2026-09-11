"""Cross-context EDA grant mutation composition tests."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from booley.eda.provisioning import authority
from booley.harness import eda_grants


def test_grant_add_delegates_required_callbacks_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = authority.ProjectGrant(str(project), "vivado", "registered", None)
    captured = {}
    monkeypatch.setattr(authority, "grant_project_identity", str)
    monkeypatch.setattr(
        authority,
        "_prepare_add_grant",
        lambda *_args, **_kwargs: nullcontext(lambda: grant),
    )

    def coordinate(**kwargs):
        captured.update(kwargs)
        identity = kwargs["resolve_project_identity"]()
        with kwargs["mutation"](identity) as commit:
            return commit()

    monkeypatch.setattr(eda_grants.issuance_invalidation, "coordinate_mutation", coordinate)

    assert (
        eda_grants.GrantCoordinator().add(
            project,
            "vivado",
            installation="registered",
            license_profile=None,
        )
        == grant
    )
    assert captured["operation"] == "EDA grant add"
    assert captured["invalidate_before_mutation"] is True
    assert captured["cleanup_after_mutation"] is False
    assert captured["cleanup_resources"] is eda_grants.cleanup_project_resources_for_identity


def test_grant_revoke_resolves_and_mutates_one_stored_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    stored = str(tmp_path / "stored-project")
    grant = authority.ProjectGrant(stored, "vivado", "registered", "site")
    mutated = []
    monkeypatch.setattr(authority, "revoke_project_identity", lambda *_args: stored)
    monkeypatch.setattr(
        authority,
        "_prepare_revoke_grant",
        lambda identity, kind: nullcontext(lambda: mutated.append((identity, kind)) or grant),
    )

    def coordinate(**kwargs):
        identity = kwargs["resolve_project_identity"]()
        assert kwargs["invalidate_before_mutation"] is False
        assert kwargs["cleanup_after_mutation"] is True
        with kwargs["mutation"](identity) as commit:
            return commit()

    monkeypatch.setattr(eda_grants.issuance_invalidation, "coordinate_mutation", coordinate)

    assert eda_grants.GrantCoordinator().revoke(project, "vivado") == grant
    assert mutated == [(stored, "vivado")]


def test_raw_grant_mutations_are_not_public() -> None:
    assert not hasattr(authority, "add_grant")
    assert not hasattr(authority, "revoke_grant")


def test_pending_recovery_probe_translates_runtime_failure(monkeypatch) -> None:
    from booley.runtime import session_refresh

    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt journal")),
    )

    with pytest.raises(authority.AuthorityError, match="corrupt journal"):
        eda_grants.GrantCoordinator().recovery_pending()
