"""Basis Refresh reconstructs only approved consumer-authored controls."""

import json
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from booley.core.models import TargetPlan, TargetPlanRole
from booley.ticket_board import basis_publication, basis_refresh
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


def test_reapply_test_tables_handles_absent_unowned_equal_and_invalid_inputs(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old/.booley_project"
    new = tmp_path / "new/.booley_project"
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    _reapply_test_tables(old.parent, new.parent, None)
    _reapply_test_tables(old.parent, new.parent, plan)
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "tests.toml").write_text("[other]\nmodule = 'same'\n", encoding="utf-8")
    (new / "tests.toml").write_text("[other]\nmodule = 'same'\n", encoding="utf-8")
    _reapply_test_tables(old.parent, new.parent, plan)
    (old / "tests.toml").write_text("[future]\nmodule = 'same'\n", encoding="utf-8")
    (new / "tests.toml").write_text("[future]\nmodule = 'same'\n", encoding="utf-8")
    _reapply_test_tables(old.parent, new.parent, plan)
    (old / "tests.toml").write_text("[broken\n", encoding="utf-8")
    with pytest.raises(BasisRefreshError, match=r"cannot recover approved tests\.toml"):
        _reapply_test_tables(old.parent, new.parent, plan)


def test_reapply_placeholders_validates_project_sources_and_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_outer = tmp_path / "old-outer"
    old_project = tmp_path / "old-project"
    new_outer = tmp_path / "new-outer"
    new_project = tmp_path / "new-project"
    old_project.mkdir()
    new_project.mkdir()
    source = old_project / "future.sv"
    source.write_text("content\n", encoding="utf-8")
    participant = replace(_participant(), role="project")
    basis = AcceptanceBasis((_participant(), participant))
    old = AuthoringWorkspace(old_outer, old_project, "b" * 40, "b" * 40)
    new = AuthoringWorkspace(new_outer, new_project, "c" * 40, "c" * 40)
    monkeypatch.setattr(
        basis_refresh,
        "_changed_paths",
        lambda repository, *_args: ("future.sv",) if repository == old_project else (),
    )

    with pytest.raises(BasisRefreshError, match="non-placeholder"):
        _reapply_placeholders(old, new, basis, "ticket")
    source.write_text("", encoding="utf-8")
    (new_project / "future.sv").write_text("occupied\n", encoding="utf-8")
    with pytest.raises(BasisRefreshError, match="now has content"):
        _reapply_placeholders(old, new, basis, "ticket")


def test_verify_providers_rejects_unaccepted_missing_export_bad_surface_and_bad_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = ProviderTargetBinding(
        "provider", "a" * 64, "acme:lib:toy:1.0#future", "persistent", "b" * 64
    )
    consumer = AcceptanceBasis((_participant(),), providers=(binding,))
    monkeypatch.setattr(basis_refresh, "find_ticket_file", lambda *_args: (None, None))
    with pytest.raises(BasisRefreshError, match="is not accepted"):
        _verify_providers(tmp_path, tmp_path, consumer)

    ticket = tmp_path / "provider.md"
    ticket.write_text("---\n---\n", encoding="utf-8")
    monkeypatch.setattr(basis_refresh, "find_ticket_file", lambda *_args: (ticket, "done"))
    monkeypatch.setattr(
        basis_refresh,
        "load_acceptance_basis",
        lambda *_args: (_ for _ in ()).throw(basis_refresh.AcceptanceBasisError("bad basis")),
    )
    with pytest.raises(BasisRefreshError, match="no valid accepted basis"):
        _verify_providers(tmp_path, tmp_path, consumer)

    provider_basis = AcceptanceBasis((_participant(),))
    monkeypatch.setattr(basis_refresh, "load_acceptance_basis", lambda *_args: provider_basis)
    with pytest.raises(BasisRefreshError, match="no longer exports"):
        _verify_providers(tmp_path, tmp_path, consumer)

    provider_basis = AcceptanceBasis(
        (_participant(),),
        target_plan=TargetPlan.from_value([{"target": binding.target, "role": "persistent"}]),
    )
    monkeypatch.setattr(basis_refresh, "load_acceptance_basis", lambda *_args: provider_basis)
    monkeypatch.setattr(basis_refresh, "target_surface_sha256", lambda *_args: "c" * 64)
    with pytest.raises(BasisRefreshError, match="changed after it was pinned"):
        _verify_providers(tmp_path, tmp_path, consumer)


def _refresh_build_fixture(
    tmp_path: Path,
) -> tuple[
    ProviderTargetBinding,
    AcceptanceBasis,
    basis_refresh.BasisRefreshJournal,
    Path,
    AuthoringWorkspace,
    basis_refresh._RefreshBuild,
]:
    provider = ProviderTargetBinding(
        "provider", "a" * 64, "acme:lib:toy:1.0#future", "persistent", "b" * 64
    )
    basis = AcceptanceBasis((_participant(),))
    journal = basis_refresh.BasisRefreshJournal(
        1, "0" * 32, "1" * 16, "ticket", basis.basis_id, "building", {}
    )
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\n---\n", encoding="utf-8")
    old = AuthoringWorkspace(tmp_path / "old", None, "b" * 40, "")
    workspace = AuthoringWorkspace(tmp_path / "new", None, "b" * 40, "", journal.generation)
    refresh = basis_refresh._RefreshBuild(tmp_path, ticket, "ticket", {}, basis, old, journal)
    return provider, basis, journal, ticket, workspace, refresh


def test_build_refresh_runs_all_reapplication_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _basis, _journal, _ticket, workspace, refresh = _refresh_build_fixture(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(basis_refresh, "open_authoring_generation", lambda *_args: workspace)
    monkeypatch.setattr(basis_refresh, "_verify_providers", lambda *_args: (provider,))
    monkeypatch.setattr(basis_refresh, "_reapply_targets", lambda *_args: calls.append("targets"))
    monkeypatch.setattr(
        basis_refresh, "_reapply_test_tables", lambda *_args: calls.append("tables")
    )
    monkeypatch.setattr(
        basis_refresh, "_reapply_placeholders", lambda *_args: calls.append("placeholders")
    )

    assert basis_refresh._build_refresh_workspace(refresh) == (workspace, (provider,))
    assert calls == ["targets", "tables", "placeholders"]


def test_publish_refresh_writes_receipt_and_resumes_prepared_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, basis, journal, ticket, workspace, _refresh = _refresh_build_fixture(tmp_path)
    receipts: list[str] = []
    journals: list[basis_refresh.BasisRefreshJournal] = []
    monkeypatch.setattr(
        basis_refresh,
        "prepare_replacement_acceptance_basis",
        lambda *_args, **_kwargs: (basis, journal.operation_id),
    )
    monkeypatch.setattr(
        basis_refresh, "write_basis_receipt", lambda *_args, **_kwargs: receipts.append("receipt")
    )
    monkeypatch.setattr(basis_refresh, "_write_journal", lambda _root, item: journals.append(item))

    published, prepared = basis_refresh._publish_refresh_basis(
        tmp_path, ticket, "ticket", workspace, (provider,), journal
    )

    assert published == basis
    assert prepared.state == "prepared"
    assert receipts == ["receipt"]
    assert journals == [prepared]
    assert basis_refresh._publish_refresh_basis(
        tmp_path, ticket, "ticket", workspace, (provider,), prepared
    ) == (basis, prepared)


def _prepared_refresh_fixture(
    tmp_path: Path,
) -> tuple[AcceptanceBasis, basis_refresh.BasisRefreshJournal, Path, Path]:
    basis = AcceptanceBasis((_participant(),))
    journal = basis_refresh.BasisRefreshJournal(
        1, "0" * 32, "1" * 16, "ticket", basis.basis_id, "prepared", basis.as_dict()
    )
    journal_path = tmp_path / "refresh.json"
    operation = tmp_path / "operation"
    journal_path.write_text("pending\n", encoding="utf-8")
    operation.mkdir()
    return basis, journal, journal_path, operation


def test_finish_refresh_validates_identity_and_cleans_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _basis, journal, journal_path, operation = _prepared_refresh_fixture(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: None)
    basis_refresh.finish_basis_refresh(tmp_path, "ticket", journal.operation_id)
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: journal)
    with pytest.raises(BasisRefreshError, match="completion identity changed"):
        basis_refresh.finish_basis_refresh(tmp_path, "ticket", "f" * 32)
    monkeypatch.setattr(basis_refresh, "_operation_path", lambda *_args: operation)
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: journal_path)
    monkeypatch.setattr(
        basis_publication,
        "finish_basis_publication",
        lambda *_args: calls.append("finish-publication"),
    )
    monkeypatch.setattr(
        basis_refresh, "safe_rmtree", lambda *_args, **_kwargs: calls.append("rmtree")
    )
    basis_refresh.finish_basis_refresh(tmp_path, "ticket", journal.operation_id)
    assert calls == ["finish-publication", "rmtree"]


def test_discard_refresh_abandons_publication_refs_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _basis, journal, journal_path, operation = _prepared_refresh_fixture(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: None)
    basis_refresh.discard_basis_refresh(tmp_path, "ticket")
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: journal)
    monkeypatch.setattr(basis_refresh, "_operation_path", lambda *_args: operation)
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: journal_path)
    repositories = {"outer": tmp_path}
    monkeypatch.setattr(basis_refresh, "discard_refresh_workspace", lambda *_args: repositories)
    monkeypatch.setattr(
        basis_publication,
        "abandon_basis_publication",
        lambda *_args: calls.append("abandon-publication"),
    )
    monkeypatch.setattr(
        basis_refresh, "discard_generation_refs", lambda *_args: calls.append("discard-refs")
    )
    monkeypatch.setattr(
        basis_refresh, "safe_rmtree", lambda *_args, **_kwargs: calls.append("rmtree")
    )
    basis_refresh.discard_basis_refresh(tmp_path, "ticket")
    assert calls == ["abandon-publication", "discard-refs", "rmtree"]


def test_recover_refresh_finishes_matching_ticket_and_rejects_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis, journal, _journal_path, _operation = _prepared_refresh_fixture(tmp_path)
    finished: list[tuple[str, str]] = []
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: journal)
    monkeypatch.setattr(
        basis_refresh,
        "finish_basis_refresh",
        lambda _root, slug, operation_id: finished.append((slug, operation_id)),
    )
    basis_refresh.recover_published_basis_refreshes(
        tmp_path,
        [
            {"status": "done", "feature_branch": "ticket"},
            {"status": "queued"},
            {"status": "queued", "feature_branch": "ticket", "acceptance_basis": basis.as_dict()},
        ],
    )
    assert finished == [("ticket", journal.operation_id)]
    with pytest.raises(BasisRefreshError, match="disagrees"):
        basis_refresh.recover_published_basis_refreshes(
            tmp_path,
            [{"status": "queued", "feature_branch": "ticket", "acceptance_basis": {}}],
        )
