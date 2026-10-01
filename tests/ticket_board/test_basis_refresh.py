"""Basis Refresh reconstructs only approved consumer-authored controls."""

import json
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from booley.core.models import TargetPlan, TargetPlanRole
from booley.runtime.project_dir import reset_cache
from booley.ticket_board import basis_publication, basis_refresh, operations
from booley.ticket_board.basis_refresh import (
    BasisRefreshError,
    _reapply_placeholders,
    _reapply_targets,
    _reapply_test_tables,
    _verify_providers,
    load_basis_refresh,
    prepare_waiting_basis_refresh,
)
from booley.ticket_board.board_layout import read_state_record
from booley.ticket_board.frontmatter import format_frontmatter
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    ProviderTargetBinding,
    TicketBaseline,
    ticket_machine_digest,
    ticket_machine_fields,
    ticket_machine_from_spec,
)
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
)
from booley.ticket_board.workspace_ops import AuthoringWorkspace
from tests.ticket_board.conftest import place_closed_ticket, place_ticket

from .conftest import make_paired_repository


def _fake_document():
    return SimpleNamespace(
        spec=SimpleNamespace(fields={}, semantic_digest=lambda: "0" * 64),
        generated={"machine": {"generation": "e" * 32}},
    )


def _write_core(path: Path, targets: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    filesets = {
        name: {}
        for target in targets.values()
        for name in target.get("filesets", [])
        if isinstance(name, str)
    }
    (path / "toy.core").write_text(
        yaml.safe_dump(
            {
                "CAPI=2": None,
                "name": "acme:lib:toy:1.0",
                "filesets": filesets,
                "targets": targets,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _ticket_identity_commit(
    root: Path,
    slug: str,
    machine: dict,
    parent: str,
) -> str:
    message = (
        f"Publish {slug}\n\n"
        f"Booley-Ticket-Slug: {slug}\n"
        f"Booley-Authored-SHA256: {machine['authored_sha256']}\n"
        f"Booley-Machine-SHA256: {ticket_machine_digest(machine)}\n"
    )
    result = subprocess.run(
        ["git", "commit-tree", f"{parent}^{{tree}}", "-p", parent],
        cwd=root,
        input=message,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _write_published_ticket(
    root: Path,
    project: Path,
    slug: str,
    status: str,
    fields: dict,
    body: str,
    generation: str,
    *,
    providers: tuple[ProviderTargetBinding, ...] = (),
) -> TicketBaseline:
    outer_destination = _git(root, "rev-parse", "main")
    project_head = _git(project, "rev-parse", "main")
    outer_ref = f"refs/heads/booley-generation/{generation[:16]}/{slug}"
    project_ref = f"refs/heads/booley-generation/{generation[:16]}/{slug}-project"
    participants = (
        BasisParticipant("outer", "0" * 40, outer_ref, "refs/heads/main", outer_destination),
        BasisParticipant("project", project_head, project_ref, "refs/heads/main", project_head),
    )
    preliminary = TicketBaseline(participants, providers=providers)
    view = TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ())
    converted = convert_ticket_document(
        format_frontmatter(fields, body),
        TicketConversionContext("draft", lambda _generated: view),
    )
    assert converted.document is not None
    machine = ticket_machine_from_spec(preliminary, converted.document.spec, generation)
    outer_head = _ticket_identity_commit(root, slug, machine, outer_destination)
    basis = replace(
        preliminary,
        participants=(replace(participants[0], authoring_sha=outer_head), participants[1]),
    )
    machine = ticket_machine_from_spec(basis, converted.document.spec, generation)
    _git(root, "update-ref", outer_ref, outer_head)
    _git(project, "update-ref", project_ref, project_head)
    content = format_frontmatter({**fields, "machine": machine}, body)
    if status == "done":
        place_closed_ticket(project / "tickets", slug, content, generation=generation)
    else:
        place_ticket(project / "tickets", slug, status, content)
    return replace(basis, machine=machine)


def _paired_refresh_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    root = tmp_path / "project"
    project = make_paired_repository(root)
    (project / "tickets").mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    reset_cache()
    return root, project


def _publish_missing_target_refresh(root: Path, project: Path) -> TicketBaseline:
    common = {
        "type": "bugfix",
        "branch": "main",
        "project_destination_ref": "refs/heads/main",
        "scope": ["README.md"],
        "on_success": ["review"],
        "CRITERIA_MANDATORY": {"REVIEW": {"rtl": {"bugs": "clean"}}},
    }
    provider_generation = "1" * 32
    _write_published_ticket(
        root,
        project,
        "provider",
        "done",
        {**common, "summary": "Accepted provider"},
        "## Description\n\nNo longer exports the Target.\n",
        provider_generation,
    )
    binding = ProviderTargetBinding(
        "provider", provider_generation, "acme:lib:toy:1.0#removed", "persistent", "2" * 64
    )
    return _write_published_ticket(
        root,
        project,
        "consumer",
        "waiting",
        {**common, "summary": "Waiting consumer", "dependencies": ["provider"]},
        "## Description\n\nRefresh after provider acceptance.\n",
        "3" * 32,
        providers=(binding,),
    )


def _remove_canonical_generation_worktree(
    root: Path, project: Path, basis: TicketBaseline
) -> None:
    canonical = project / "worktrees/consumer"
    canonical.parent.mkdir(parents=True)
    _git(
        root,
        "worktree",
        "add",
        "-q",
        "--detach",
        str(canonical),
        basis.participant("outer").authoring_sha,
    )
    nested = canonical / ".booley_project"
    _git(
        project,
        "worktree",
        "add",
        "-q",
        "--detach",
        str(nested),
        basis.participant("project").authoring_sha,
    )
    assert nested.is_dir()
    _git(project, "worktree", "remove", "--force", str(nested))
    _git(root, "worktree", "remove", "--force", str(canonical))
    assert not canonical.exists()


def test_promotion_reconstructs_paired_basis_before_provider_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, project = _paired_refresh_repositories(tmp_path, monkeypatch)
    basis = _publish_missing_target_refresh(root, project)
    _remove_canonical_generation_worktree(root, project, basis)
    board = TicketIO(project / "tickets", project_root=root)

    assert _git(root, "status", "--porcelain", "--untracked-files=all") == ""
    assert operations.op_promote_waiting(board) == []

    error = capsys.readouterr().err
    assert "provider Ticket 'provider' no longer exports 'acme:lib:toy:1.0#removed'" in error
    assert read_state_record(project / "tickets", "consumer").state is TicketState.BLOCKED
    reconstructed = next((project / ".runtime/acceptance/refresh").glob("*/old-basis"))
    assert "/.booley_project" not in (reconstructed / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    )
    assert _git(reconstructed, "status", "--porcelain", "--untracked-files=all") == (
        "?? .booley_project/"
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


def test_reapply_targets_recovers_only_candidate_referenced_filesets(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "toy.core").write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\nfilesets:\n"
        "  candidate_inputs: {files: [candidate.sv]}\n"
        "  stale_inputs: {files: [stale.sv]}\n"
        "targets:\n  consumer: {filesets: [candidate_inputs]}\n"
        "  stale: {filesets: [stale_inputs]}\n",
        encoding="utf-8",
    )
    (new / "toy.core").write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\nfilesets:\n"
        "  current_inputs: {files: [current.sv]}\n"
        "targets:\n  current: {filesets: [current_inputs]}\n",
        encoding="utf-8",
    )
    plan = TargetPlan.from_value([{"target": "consumer", "role": "persistent"}])

    _reapply_targets(old, new, plan)

    document = yaml.safe_load((new / "toy.core").read_text(encoding="utf-8"))
    assert set(document["targets"]) == {"current", "consumer"}
    assert set(document["filesets"]) == {"current_inputs", "candidate_inputs"}


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


def test_reapply_placeholders_rejects_old_basis_record(tmp_path: Path, monkeypatch) -> None:
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

    with pytest.raises(BasisRefreshError, match="non-placeholder content"):
        _reapply_placeholders(
            AuthoringWorkspace(old, None, "b" * 40, ""),
            AuthoringWorkspace(new, None, "c" * 40, ""),
            TicketBaseline((participant,)),
            "ticket",
        )

    assert not (new / "rtl" / "future.sv").exists()
    assert not (new / ".booley_project" / "acceptance").exists()


@pytest.mark.parametrize("accepted_generation", ["a" * 32, "c" * 32])
def test_provider_refresh_requires_pinned_generation_even_with_same_surface(
    tmp_path: Path, monkeypatch, accepted_generation: str
) -> None:
    binding = ProviderTargetBinding(
        "provider",
        "a" * 32,
        "acme:lib:toy:1.0#future",
        TargetPlanRole.PERSISTENT.value,
        "b" * 64,
    )
    consumer = TicketBaseline((_participant(),), providers=(binding,))
    refreshed_provider = SimpleNamespace(
        ticket_identity=lambda: {"generation": accepted_generation},
        target_plan=TargetPlan.from_value(
            [
                {
                    "target": "acme:lib:toy:1.0#future",
                    "role": "persistent",
                }
            ]
        ),
    )
    place_closed_ticket(_tickets_dir(tmp_path), "provider", "---\n---\n")
    monkeypatch.setattr(basis_refresh, "_converted_text", lambda *_args: _fake_document())
    monkeypatch.setattr(
        basis_refresh, "load_ticket_baseline_from_document", lambda *_args: refreshed_provider
    )
    monkeypatch.setattr(basis_refresh, "target_surface_sha256", lambda *_args: "b" * 64)

    if accepted_generation != binding.ticket_generation:
        with pytest.raises(BasisRefreshError, match="changed generation"):
            _verify_providers(tmp_path, tmp_path, consumer)
    else:
        assert _verify_providers(tmp_path, tmp_path, consumer) == (binding,)


def _tickets_dir(root: Path) -> Path:
    """Return the tickets directory _verify_providers resolves for checkout *root*."""
    project = root / ".booley_project"
    project.mkdir(exist_ok=True)
    return project / "tickets"


def _participant() -> BasisParticipant:
    return BasisParticipant(
        "outer",
        "a" * 40,
        "refs/heads/booley-generation/0123456789abcdef/ticket",
        "refs/heads/main",
        "b" * 40,
    )


def _published_basis(*, providers: tuple[ProviderTargetBinding, ...] = ()) -> TicketBaseline:
    basis = TicketBaseline((_participant(),), providers=providers)
    return replace(
        basis,
        machine=ticket_machine_fields(basis, fields={}, body="", generation="e" * 32),
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
                "old_ticket_generation": "b" * 32,
                "state": "building",
                "machine": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: path)

    with pytest.raises(BasisRefreshError, match="journal is invalid"):
        load_basis_refresh(tmp_path, "ticket")


def _stub_refresh_recovery(tmp_path: Path, monkeypatch):
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        format_frontmatter({"machine": {"generation": "e" * 32}}, ""),
        encoding="utf-8",
    )
    provider = ProviderTargetBinding(
        "provider", "c" * 32, "acme:lib:toy:1.0#future", "persistent", "d" * 64
    )
    old_basis = _published_basis(providers=(provider,))
    new_basis = TicketBaseline((_participant(),), providers=(provider,))
    journal_path = tmp_path / "refresh.json"
    operations = tmp_path / "operations"
    monkeypatch.setattr(basis_refresh, "_journal_path", lambda *_args: journal_path)
    monkeypatch.setattr(
        basis_refresh, "_operation_path", lambda _root, operation: operations / operation
    )
    monkeypatch.setattr(basis_refresh, "resolve_project_dir", lambda root: root)
    monkeypatch.setattr(basis_refresh, "_converted_ticket", lambda *_args: _fake_document())
    monkeypatch.setattr(
        basis_refresh,
        "load_ticket_baseline_from_document",
        lambda _root, _slug, document: (
            old_basis
            if document.generated.get("machine", {}).get("generation") == "e" * 32
            else new_basis
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
        "prepare_replacement_ticket_baseline",
        lambda *_args, **kwargs: (new_basis, kwargs["operation_id"]),
    )
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

    assert recovered.participants == new_basis.participants
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
    basis = TicketBaseline((_participant(), participant))
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
        "provider", "a" * 32, "acme:lib:toy:1.0#future", "persistent", "b" * 64
    )
    consumer = TicketBaseline((_participant(),), providers=(binding,))
    tickets = _tickets_dir(tmp_path)
    # A provider that never closed is not accepted, even with a live board document.
    place_ticket(tickets, "provider", "done", "---\n---\n")
    with pytest.raises(BasisRefreshError, match="is not accepted"):
        _verify_providers(tmp_path, tmp_path, consumer)
    (tickets / "board" / "provider.md").unlink()

    # Only outcome done accepts a provider; an archived one never does.
    history = place_closed_ticket(tickets, "provider", "---\n---\n", outcome="archived")
    with pytest.raises(BasisRefreshError, match="is not accepted"):
        _verify_providers(tmp_path, tmp_path, consumer)

    # A malformed history record fails closed instead of reading as absent.
    history.write_text("---\nclosed:\n  outcome: bogus\n---\n", encoding="utf-8")
    with pytest.raises(BasisRefreshError, match="history document"):
        _verify_providers(tmp_path, tmp_path, consumer)

    authored = "---\nsummary: Provider\n---\n## Description\nProvide.\n"
    place_closed_ticket(tickets, "provider", authored)
    converted: list[str] = []

    def convert(_root: Path, text: str, slug: str) -> SimpleNamespace:
        assert slug == "provider"
        converted.append(text)
        return _fake_document()

    monkeypatch.setattr(basis_refresh, "_converted_text", convert)
    monkeypatch.setattr(
        basis_refresh,
        "load_ticket_baseline_from_document",
        lambda *_args: (_ for _ in ()).throw(basis_refresh.TicketBaselineError("bad basis")),
    )
    with pytest.raises(BasisRefreshError, match="no valid accepted basis"):
        _verify_providers(tmp_path, tmp_path, consumer)
    # The provider basis comes from the document as it closed, without the closed block.
    assert converted == [authored]

    provider_basis = TicketBaseline((_participant(),))
    monkeypatch.setattr(
        basis_refresh, "load_ticket_baseline_from_document", lambda *_args: provider_basis
    )
    with pytest.raises(BasisRefreshError, match="no longer exports"):
        _verify_providers(tmp_path, tmp_path, consumer)

    provider_basis = TicketBaseline(
        (_participant(),),
        target_plan=TargetPlan.from_value([{"target": binding.target, "role": "persistent"}]),
    )
    monkeypatch.setattr(
        basis_refresh, "load_ticket_baseline_from_document", lambda *_args: provider_basis
    )
    monkeypatch.setattr(basis_refresh, "target_surface_sha256", lambda *_args: "c" * 64)
    with pytest.raises(BasisRefreshError, match="changed after it was pinned"):
        _verify_providers(tmp_path, tmp_path, consumer)


def _refresh_build_fixture(
    tmp_path: Path,
) -> tuple[
    ProviderTargetBinding,
    TicketBaseline,
    basis_refresh.BasisRefreshJournal,
    Path,
    AuthoringWorkspace,
    basis_refresh._RefreshBuild,
]:
    provider = ProviderTargetBinding(
        "provider", "a" * 32, "acme:lib:toy:1.0#future", "persistent", "b" * 64
    )
    basis = _published_basis()
    journal = basis_refresh.BasisRefreshJournal(
        1, "0" * 32, "1" * 16, "ticket", basis.ticket_identity()["generation"], "building", {}
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


def test_publish_refresh_records_machine_identity_and_resumes_prepared_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, basis, journal, ticket, workspace, _refresh = _refresh_build_fixture(tmp_path)
    journals: list[basis_refresh.BasisRefreshJournal] = []
    monkeypatch.setattr(
        basis_refresh,
        "prepare_replacement_ticket_baseline",
        lambda *_args, **_kwargs: (basis, journal.operation_id),
    )
    monkeypatch.setattr(basis_refresh, "_converted_ticket", lambda *_args: _fake_document())
    monkeypatch.setattr(basis_refresh, "_write_journal", lambda _root, item: journals.append(item))

    published, prepared = basis_refresh._publish_refresh_basis(
        tmp_path, ticket, "ticket", workspace, (provider,), journal
    )

    assert published == basis
    assert prepared.state == "prepared"
    assert prepared.machine["generation"] == journal.operation_id
    assert journals == [prepared]
    resumed, same_journal = basis_refresh._publish_refresh_basis(
        tmp_path, ticket, "ticket", workspace, (provider,), prepared
    )
    assert resumed.participants == basis.participants
    assert same_journal == prepared


def _prepared_refresh_fixture(
    tmp_path: Path,
) -> tuple[TicketBaseline, basis_refresh.BasisRefreshJournal, Path, Path]:
    basis = _published_basis()
    journal = basis_refresh.BasisRefreshJournal(
        1,
        "0" * 32,
        "1" * 16,
        "ticket",
        basis.ticket_identity()["generation"],
        "prepared",
        basis.ticket_identity(),
    )
    journal_path = tmp_path / "refresh.json"
    operation = tmp_path / "operation"
    journal_path.write_text("pending\n", encoding="utf-8")
    operation.mkdir()
    return basis, journal, journal_path, operation


def test_refresh_rejects_stale_waiting_ticket_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis, journal, _path, _operation = _prepared_refresh_fixture(tmp_path)
    stale = replace(journal, old_ticket_generation="f" * 32)
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: stale)
    with pytest.raises(BasisRefreshError, match="waiting Ticket changed"):
        basis_refresh._new_journal(tmp_path, "ticket", basis)


def test_refresh_rejects_invalid_prepared_machine_and_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board.ticket_baseline import TicketBaselineError

    _basis, journal, _path, _operation = _prepared_refresh_fixture(tmp_path)
    with pytest.raises(BasisRefreshError, match="invalid new basis"):
        basis_refresh._validate_journal(
            replace(journal, machine={}), "ticket", tmp_path / "refresh.json"
        )
    monkeypatch.setattr(
        basis_refresh,
        "load_ticket_baseline_from_document",
        lambda *_args: (_ for _ in ()).throw(TicketBaselineError("Ticket changed")),
    )
    monkeypatch.setattr(basis_refresh, "_converted_ticket", lambda *_args: _fake_document())
    with pytest.raises(BasisRefreshError, match="Ticket changed"):
        basis_refresh._resume_prepared_refresh(tmp_path, "ticket", _fake_document(), journal)
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: None)
    with pytest.raises(BasisRefreshError, match="Ticket changed"):
        prepare_waiting_basis_refresh(tmp_path, ticket, "ticket")


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
    monkeypatch.setattr(
        basis_refresh,
        "_recover_published_refresh",
        lambda root, slug, journal: basis_refresh.finish_basis_refresh(
            root, slug, journal.operation_id
        ),
    )
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
            {"status": "queued", "feature_branch": "ticket", "machine": basis.ticket_identity()},
        ],
    )
    assert finished == [("ticket", journal.operation_id)]
    with pytest.raises(BasisRefreshError, match="disagrees"):
        basis_refresh.recover_published_basis_refreshes(
            tmp_path,
            [{"status": "queued", "feature_branch": "ticket", "machine": {}}],
        )
