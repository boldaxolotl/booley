"""Basis Refresh reconstructs only approved consumer-authored controls."""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from booley.core.models import TargetPlan, TargetPlanRole
from booley.ticket_board import basis_refresh
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
    ProviderTargetBinding,
)
from booley.ticket_board.basis_refresh import (
    BasisRefreshError,
    _reapply_placeholders,
    _reapply_targets,
    _reapply_test_tables,
    _verify_providers,
    load_basis_refresh,
    prepare_waiting_basis_refresh,
)
from booley.ticket_board.workspace_ops import AuthoringWorkspace


def _write_core(path: Path, targets: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "toy.core").write_text(
        yaml.safe_dump(
            {"CAPI=2": None, "name": "acme:lib:toy:1.0", "targets": targets},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_reapply_targets_keeps_current_destination_and_approved_candidate(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_core(
        old,
        {
            "accepted_provider": {"filesets": ["provider"]},
            "provider_ephemeral": {"filesets": ["probe"]},
            "consumer": {"filesets": ["consumer"]},
        },
    )
    _write_core(
        new,
        {
            "accepted_provider": {"filesets": ["provider"]},
            "concurrent": {"filesets": ["other"]},
        },
    )
    plan = TargetPlan.from_value([{"target": "consumer", "role": "persistent"}])

    _reapply_targets(old, new, plan)

    targets = yaml.safe_load((new / "toy.core").read_text(encoding="utf-8"))["targets"]
    assert set(targets) == {"accepted_provider", "concurrent", "consumer"}
    assert "provider_ephemeral" not in targets


def test_reapply_test_tables_rejects_current_destination_conflict(tmp_path: Path) -> None:
    old = tmp_path / "old/.booley_project"
    new = tmp_path / "new/.booley_project"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "tests.toml").write_text("[future]\nmodule = 'accepted'\n", encoding="utf-8")
    (new / "tests.toml").write_text("[future]\nmodule = 'concurrent'\n", encoding="utf-8")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])

    with pytest.raises(BasisRefreshError, match="conflicts with current destination"):
        _reapply_test_tables(old.parent, new.parent, plan)


def test_reapply_test_tables_includes_nested_owned_tables(tmp_path: Path) -> None:
    old = tmp_path / "old/.booley_project"
    new = tmp_path / "new/.booley_project"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "tests.toml").write_text(
        "[future]\nmodule = 'accepted'\n[future.env]\nMODE = 'fast'\n",
        encoding="utf-8",
    )
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])

    _reapply_test_tables(old.parent, new.parent, plan)
    tables = tomllib.loads((new / "tests.toml").read_text(encoding="utf-8"))

    assert tables["future"]["env"] == {"MODE": "fast"}


def test_reapply_placeholders_skips_machine_owned_basis_record(
    tmp_path: Path, monkeypatch
) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    record = old / ".booley_project" / "acceptance" / "bases" / "ticket.json"
    record.parent.mkdir(parents=True)
    record.write_text("machine owned\n", encoding="utf-8")
    placeholder = old / "rtl" / "future.sv"
    placeholder.parent.mkdir()
    placeholder.touch()
    monkeypatch.setattr(
        basis_refresh,
        "_changed_paths",
        lambda *_args: (
            ".booley_project/acceptance/bases/ticket.json",
            "rtl/future.sv",
        ),
    )
    participant = BasisParticipant(
        "outer",
        "a" * 40,
        "refs/heads/booley-generation/0123456789abcdef/ticket",
        "refs/heads/main",
        "b" * 40,
    )

    _reapply_placeholders(
        AuthoringWorkspace(old, None, "b" * 40, ""),
        AuthoringWorkspace(new, None, "c" * 40, ""),
        AcceptanceBasis((participant,)),
        "ticket",
    )

    assert (new / "rtl" / "future.sv").is_file()
    assert not (new / ".booley_project" / "acceptance").exists()


def test_provider_basis_refresh_is_allowed_when_pinned_export_is_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    binding = ProviderTargetBinding(
        "provider",
        "a" * 64,
        "acme:lib:toy:1.0#future",
        TargetPlanRole.PERSISTENT.value,
        "b" * 64,
    )
    consumer = AcceptanceBasis((_participant(),), providers=(binding,))
    refreshed_provider = SimpleNamespace(
        basis_id="c" * 64,
        target_plan=TargetPlan.from_value(
            [
                {
                    "target": "acme:lib:toy:1.0#future",
                    "role": "persistent",
                }
            ]
        ),
    )
    ticket = tmp_path / "provider.md"
    ticket.write_text("---\n---\n", encoding="utf-8")
    monkeypatch.setattr(basis_refresh, "find_ticket_file", lambda *_args: (ticket, "done"))
    monkeypatch.setattr(basis_refresh, "load_acceptance_basis", lambda *_args: refreshed_provider)
    monkeypatch.setattr(basis_refresh, "target_surface_sha256", lambda *_args: "b" * 64)

    refreshed = _verify_providers(tmp_path, tmp_path, consumer)

    assert refreshed[0].basis_id == "c" * 64


def _participant() -> BasisParticipant:
    return BasisParticipant(
        "outer",
        "a" * 40,
        "refs/heads/booley-generation/0123456789abcdef/ticket",
        "refs/heads/main",
        "b" * 40,
    )


def test_journal_boundary_rejects_non_string_identity(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "refresh.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": [],
                "generation": "a" * 16,
                "slug": "ticket",
                "old_basis_id": "b" * 64,
                "state": "building",
                "new_basis": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: path)

    with pytest.raises(BasisRefreshError, match="journal is invalid"):
        load_basis_refresh(tmp_path, "ticket")


def _stub_refresh_recovery(tmp_path: Path, monkeypatch):
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nacceptance_basis: {}\n---\n", encoding="utf-8")
    provider = ProviderTargetBinding(
        "provider", "c" * 64, "acme:lib:toy:1.0#future", "persistent", "d" * 64
    )
    old_basis = AcceptanceBasis((_participant(),), providers=(provider,))
    new_basis = AcceptanceBasis((_participant(),), providers=(provider,))
    journal_path = tmp_path / "refresh.json"
    operations = tmp_path / "operations"
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: journal_path)
    monkeypatch.setattr(
        basis_refresh, "_operation_path", lambda _root, operation: operations / operation
    )
    monkeypatch.setattr(basis_refresh, "resolve_project_dir", lambda root: root)
    monkeypatch.setattr(
        basis_refresh,
        "load_acceptance_basis",
        lambda _root, _slug, fields, _body: (
            new_basis if fields.get("acceptance_basis") not in ({}, None) else old_basis
        ),
    )

    monkeypatch.setattr(
        basis_refresh,
        "load_refresh_source_workspace",
        lambda *_args: AuthoringWorkspace(tmp_path / "old", None, "b" * 40, ""),
    )
    workspace = AuthoringWorkspace(tmp_path / "new", None, "b" * 40, "", "a" * 16)
    monkeypatch.setattr(
        basis_refresh, "_build_refresh_workspace", lambda _refresh: (workspace, (provider,))
    )
    monkeypatch.setattr(
        basis_refresh,
        "prepare_replacement_acceptance_basis",
        lambda *_args, **kwargs: (new_basis, kwargs["operation_id"]),
    )
    monkeypatch.setattr(basis_refresh, "write_basis_receipt", lambda *_args, **_kwargs: {})
    calls = []

    def fail_once(*args, **_kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("relocation interrupted")

    monkeypatch.setattr(basis_refresh, "relocate_refresh_workspace", fail_once)
    return ticket, new_basis, calls


def test_public_refresh_recovers_after_prepared_relocation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    ticket, new_basis, calls = _stub_refresh_recovery(tmp_path, monkeypatch)

    with pytest.raises(BasisRefreshError, match="relocation interrupted"):
        prepare_waiting_basis_refresh(tmp_path, ticket, "ticket")
    recovered, operation = prepare_waiting_basis_refresh(tmp_path, ticket, "ticket")

    assert recovered == new_basis
    assert operation
    assert len(calls) == 2
