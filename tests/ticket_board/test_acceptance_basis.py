"""Acceptance Basis schema and hard-cutoff behavior."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime.project_dir import reset_cache
from booley.targets.declared_inputs import referenced_program_paths
from booley.ticket_board import (
    acceptance_basis as acceptance_basis_module,
)
from booley.ticket_board import (
    acceptance_targets,
    basis_publication,
    draft_transition,
    enqueue_publication,
    workspace_ops,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    AcceptancePathPolicy,
    BasisParticipant,
    assert_inputs_unchanged,
    authored_ticket_record,
    load_acceptance_basis,
    load_basis_receipt,
    load_basis_record,
    validate_current_basis_refs,
)
from booley.ticket_board.acceptance_journal import JournalState
from booley.ticket_board.acceptance_targets import (
    AcceptanceTargetBinding,
    validate_binding_selectors,
)
from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role=role,
        authoring_sha="a" * 40,
        ticket_ref=f"refs/heads/booley-generation/0123456789abcdef/{role}",
        destination_ref="refs/heads/main",
        destination_sha="b" * 40,
    )


def test_minimal_basis_round_trips_through_ticket_frontmatter() -> None:
    basis = AcceptanceBasis((_participant(),))
    text = format_frontmatter(
        {"summary": "demo", "acceptance_basis": basis.as_dict()},
        "## Description\n\nDemo.\n",
    )

    fields, _body = parse_frontmatter(text)

    assert AcceptanceBasis.from_mapping(fields["acceptance_basis"]) == basis
    assert set(fields["acceptance_basis"]) == {"schema", "participants"}


@pytest.mark.parametrize("schema", [True, 1.0])
def test_basis_constructor_rejects_non_integer_schema(schema: object) -> None:
    with pytest.raises(AcceptanceBasisError, match="schema must be 1"):
        AcceptanceBasis((_participant(),), schema=schema)  # type: ignore[arg-type]


@pytest.mark.parametrize("section", ["mandatory", "optional"])
def test_canonical_binding_preserves_full_criterion_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda _root, target: SimpleNamespace(identity=f"acme:lib:toy#{target}", selector=target),
    )
    specification = acceptance_targets.CriterionTarget(
        section,
        "synthesis_ok",
        "synth_toy",
        "synth",
        False,
    )

    (binding,) = acceptance_targets.canonical_acceptance_bindings(tmp_path, (specification,))

    assert binding.criterion == f"criteria.{section}.synthesis_ok"


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"schema": 2, "participants": []}, "schema must be 1"),
        ({"schema": True, "participants": []}, "must be an integer"),
        ({"schema": 1, "participants": [], "digest": "x"}, "exactly"),
        (
            {
                "schema": 1,
                "participants": [
                    {
                        **_participant().as_dict(),
                        "ticket_ref": "refs/heads/legacy-ticket",
                    }
                ],
            },
            "generation-qualified",
        ),
        (
            {
                "schema": 1,
                "participants": [
                    _participant("project").as_dict(),
                    _participant("outer").as_dict(),
                ],
            },
            "sorted by unique role",
        ),
    ],
)
def test_basis_rejects_noncanonical_frontmatter(mapping: object, message: str) -> None:
    with pytest.raises(AcceptanceBasisError, match=message):
        AcceptanceBasis.from_mapping(mapping)


def test_authored_record_rejects_legacy_target_contract() -> None:
    with pytest.raises(AcceptanceBasisError, match="hard cutoff"):
        authored_ticket_record(
            {"summary": "old", "target_contract": {"schema": 4}},
            "body",
            (),
        )


def test_authored_record_pins_complete_ticket_projection() -> None:
    fields = {
        "summary": "demo",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/demo.sv"],
        "criteria": {"mandatory": {"review_rtl_bugs": True}},
        "on_success": {"destination": "review", "remove_targets": []},
        "priority": "medium",
    }

    record = authored_ticket_record(fields, "exact body", ())

    assert record["schema"] == 1
    pinned = record["ticket"]["frontmatter"]
    assert pinned["summary"] == fields["summary"]
    assert pinned["criteria"] == fields["criteria"]
    assert pinned["dependencies"] == []
    assert pinned["spec"] == ""
    assert pinned["on_success"]["merge"] is True
    assert record["ticket"]["body"] == "exact body"


def test_authored_record_rejects_malformed_remove_targets() -> None:
    with pytest.raises(AcceptanceBasisError, match="remove_targets"):
        authored_ticket_record(
            {
                "summary": "demo",
                "type": "feature",
                "branch": "main",
                "on_success": {"remove_targets": 7},
            },
            "body",
            (),
        )


def test_record_rejects_boolean_schema_and_empty_binding() -> None:
    record = authored_ticket_record(
        {"summary": "demo", "type": "feature", "branch": "main"},
        "body",
        (),
    )
    record["schema"] = True
    with pytest.raises(AcceptanceBasisError, match="must be an integer"):
        acceptance_basis_module._validate_record(record)

    record["schema"] = 1
    record["bindings"] = [
        {
            "flow": "",
            "criterion": "criteria.mandatory.synthesis_ok",
            "baseline_identity": "acme:lib:toy#synth",
            "baseline_selector": "synth",
            "candidate_identity": "acme:lib:toy#synth",
            "candidate_selector": "synth",
        }
    ]
    with pytest.raises(AcceptanceBasisError, match="flow must be a non-empty string"):
        acceptance_basis_module._validate_record(record)


def test_committed_record_must_match_project_participant_destination() -> None:
    basis = AcceptanceBasis((_participant("outer"), _participant("project")))
    record = {
        "ticket": {
            "frontmatter": {
                "branch": "main",
                "project_destination_ref": "refs/heads/alternate",
                "on_success": {},
            }
        },
        "bindings": [],
    }

    with pytest.raises(AcceptanceBasisError, match="project destination disagrees"):
        basis.with_record(record)


def test_no_manual_contract_commands_remain() -> None:
    from booley.ticket_board.cli import build_parser

    help_text = build_parser().format_help()
    assert "contract-open" not in help_text
    assert "contract-seal" not in help_text
    assert "revise-contract" not in help_text
    assert "return-to-draft" in help_text


def test_packaged_ticket_template_has_no_generated_basis_fields() -> None:
    template = (
        Path(__file__).parents[2]
        / "src/booley/data/skills/booley-ticket-create/TICKET_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    assert "target_contract:" not in template
    assert "base_sha:" not in template


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _basis_project(tmp_path: Path) -> tuple[Path, Path, TicketIO]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    return root, project_dir, TicketIO(project_dir / "tickets", project_root=root)


def _paired_basis_project(tmp_path: Path) -> tuple[Path, Path, TicketIO]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("/.booley_project\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial outer")

    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    _git(project_dir, "init", "-b", "main")
    _git(project_dir, "config", "user.name", "Test")
    _git(project_dir, "config", "user.email", "test@example.invalid")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-m", "initial project")
    return root, project_dir, TicketIO(project_dir / "tickets", project_root=root)


def test_create_persists_inferred_paired_destination_ref(tmp_path: Path) -> None:
    _root, _project_dir, tio = _paired_basis_project(tmp_path)

    ticket = tio.create_ticket_file(
        "paired-destination",
        TicketFileSpec(
            summary="Persist paired destination",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )

    assert ticket is not None
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert fields["project_destination_ref"] == "refs/heads/main"


def test_create_rejects_missing_inferred_paired_destination_branch(tmp_path: Path) -> None:
    _root, project_dir, tio = _paired_basis_project(tmp_path)

    ticket = tio.create_ticket_file(
        "missing-paired-destination",
        TicketFileSpec(
            summary="Missing paired destination",
            ticket_type="feature",
            branch="release",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )

    assert ticket is None
    assert not (project_dir / "tickets/board/drafts/missing-paired-destination.md").exists()


def test_enqueue_automatically_publishes_basis_record_and_receipt(tmp_path: Path) -> None:
    root, project_dir, tio = _basis_project(tmp_path)

    ticket = tio.create_ticket_file(
        "automatic-basis",
        TicketFileSpec(
            summary="Publish automatically",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nExercise automatic basis publication.\n",
        ),
    )
    assert ticket is not None
    assert (project_dir / "worktrees" / "automatic-basis").is_dir()

    assert tio.enqueue_ticket("automatic-basis") is True

    queued = project_dir / "tickets" / "board" / "queue" / "automatic-basis.md"
    fields, body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    assert "target_contract" not in fields
    assert "base_sha" not in fields
    assert basis.participant("outer").ticket_ref.startswith("refs/heads/booley-generation/")
    record = project_dir / "worktrees" / "automatic-basis" / ".booley_project"
    record = record / "acceptance" / "bases" / "automatic-basis.json"
    assert record.is_file()
    receipt = project_dir / ".runtime" / "acceptance" / "bases" / "automatic-basis"
    assert (receipt / f"{basis.basis_id}.json").is_file()
    keepalive = f"refs/booley/bases/{basis.basis_id}/outer"
    assert _git(root, "rev-parse", keepalive) == basis.outer_sha
    loaded = load_acceptance_basis(root, "automatic-basis", fields, body)
    assert loaded.basis_id == basis.basis_id

    evidence = load_basis_receipt(root, "automatic-basis", basis.as_dict())
    assert evidence["basis_id"] == basis.basis_id
    assert evidence["record"]["sha256"]
    assert len(evidence["source_sha256"]) == 64
    assert len(evidence["operation_id"]) == 32


def test_current_basis_validation_rejects_rewritten_destination_ref(tmp_path: Path) -> None:
    root, _project_dir, tio = _basis_project(tmp_path)
    ticket = tio.create_ticket_file(
        "rewritten-destination",
        TicketFileSpec(
            summary="Reject rewritten destination",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("rewritten-destination") is True
    basis = tio.load_basis("rewritten-destination")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    unrelated = subprocess.run(
        ["git", "commit-tree", tree],
        cwd=root,
        input="unrelated destination\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(root, "update-ref", "refs/heads/main", unrelated)

    with pytest.raises(AcceptanceBasisError, match="no longer descends"):
        validate_current_basis_refs(root, basis)


def test_validate_ticket_recreates_missing_authoring_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board.cli import main

    root, project_dir, tio = _basis_project(tmp_path)
    ticket = tio.create_ticket_file(
        "validate-workspace",
        TicketFileSpec(
            summary="Validate from workspace",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nValidate the draft.\n",
        ),
    )
    assert ticket is not None
    workspace = project_dir / "worktrees/validate-workspace"
    _git(root, "worktree", "remove", "--force", str(workspace))
    assert not workspace.exists()
    monkeypatch.chdir(root)

    assert main(["validate-ticket", str(ticket), "--check-git"]) == 0
    assert workspace.is_dir()


def test_return_to_draft_preserves_old_ref_and_allocates_new_generation(
    tmp_path: Path,
) -> None:
    root, project_dir, tio = _basis_project(tmp_path)
    ticket = tio.create_ticket_file(
        "new-generation",
        TicketFileSpec(
            summary="Start again",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nStart again.\n",
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("new-generation")
    (tio.logs_dir / "new-generation/.runtime/ticket.lock").unlink(missing_ok=True)
    queued = project_dir / "tickets" / "board" / "queue" / "new-generation.md"
    fields, _body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    old_basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    blocked = queued.parent.parent / "blocked" / queued.name
    blocked.parent.mkdir(parents=True)
    queued.rename(blocked)

    reopened = tio.return_to_draft("new-generation")

    draft = project_dir / "tickets" / "board" / "drafts" / "new-generation.md"
    draft_fields, _body = parse_frontmatter(draft.read_text(encoding="utf-8"))
    assert "acceptance_basis" not in draft_fields
    assert "created" not in draft_fields
    assert reopened["generation"] not in old_basis.participant("outer").ticket_ref
    assert _git(root, "rev-parse", old_basis.participant("outer").ticket_ref)
    keepalive = f"refs/booley/bases/{old_basis.basis_id}/outer"
    assert _git(root, "rev-parse", keepalive) == old_basis.outer_sha
    archived_worktree = _git(root, "worktree", "list", "--porcelain")
    assert old_basis.participant("outer").ticket_ref in archived_worktree

    assert tio.enqueue_ticket("new-generation")
    (tio.logs_dir / "new-generation/.runtime/ticket.lock").unlink(missing_ok=True)
    queued_again = project_dir / "tickets/board/queue/new-generation.md"
    blocked_again = project_dir / "tickets/board/blocked/new-generation.md"
    queued_again.replace(blocked_again)
    reopened_again = tio.return_to_draft("new-generation")
    assert reopened_again["generation"] != reopened["generation"]


def _prepared_ticket(tmp_path: Path, slug: str = "transaction") -> tuple[Path, Path, TicketIO]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n/hooks/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    tio = TicketIO(project_dir / "tickets", project_root=root)
    created = tio.create_ticket_file(
        slug,
        TicketFileSpec(
            summary="Recover publication",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nRecover publication.\n",
        ),
    )
    assert created is not None
    return root, project_dir, tio


def _blocked_ticket(tmp_path: Path, slug: str = "blocked-again") -> tuple[Path, Path, TicketIO]:
    root, project_dir, tio = _prepared_ticket(tmp_path, slug)
    assert tio.enqueue_ticket(slug)
    (tio.logs_dir / slug / ".runtime/ticket.lock").unlink(missing_ok=True)
    queued = project_dir / "tickets" / "board" / "queue" / f"{slug}.md"
    blocked = queued.parent.parent / "blocked" / queued.name
    blocked.parent.mkdir(parents=True)
    queued.replace(blocked)
    return root, blocked, tio


def test_invalid_enqueue_does_not_publish_basis_artifacts(tmp_path: Path) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)

    assert tio.enqueue_ticket("transaction", on_success={"destination": "invalid"}) is False

    record = project_dir / "worktrees/transaction/.booley_project/acceptance/bases"
    assert not (record / "transaction.json").exists()
    assert not (project_dir / ".runtime/acceptance/bases/transaction").exists()
    assert "refs/booley/bases/" not in _git(root, "for-each-ref", "--format=%(refname)")
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()


def test_enqueue_force_stages_ignored_manifest_input(tmp_path: Path) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    workspace = project_dir / "worktrees/transaction"
    config = workspace / ".booley_project/booley.toml"
    hook = workspace / ".booley_project/hooks/run.py"
    config.write_text(
        '[flows]\n\n[targets.repro]\npre_run = "hooks/run.py"\n',
        encoding="utf-8",
    )
    hook.parent.mkdir(parents=True)
    hook.write_text('print("run")\n', encoding="utf-8")
    assert _git(workspace, "check-ignore", ".booley_project/hooks/run.py")

    assert tio.enqueue_ticket("transaction") is True

    basis = tio.load_basis("transaction")
    result = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            f"{basis.outer_sha}:.booley_project/hooks/run.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0


def test_enqueue_retry_finishes_interrupted_board_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    publish = enqueue_publication.publish_enqueue
    interrupted = False

    def interrupt(*args, **kwargs):
        nonlocal interrupted
        result = publish(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("after Board cutover")
        return result

    monkeypatch.setattr(enqueue_publication, "publish_enqueue", interrupt)
    with pytest.raises(OSError, match="Board cutover"):
        tio.enqueue_ticket("transaction")
    monkeypatch.setattr(enqueue_publication, "publish_enqueue", publish)

    assert tio.enqueue_ticket("transaction") is True
    queued = project_dir / "tickets/board/queue/transaction.md"
    assert queued.exists()
    transitions = project_dir / "tickets/logs/transaction/human-logs/transitions.log"
    assert transitions.read_text(encoding="utf-8").count("enqueue operation") == 1


def test_enqueue_retry_rolls_forward_after_ticket_ref_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    write_journal = basis_publication._write
    interrupted = False

    def interrupt_after_ref(project_root, journal):
        nonlocal interrupted
        if journal.published and not interrupted:
            interrupted = True
            raise OSError("after Ticket ref publication")
        write_journal(project_root, journal)

    monkeypatch.setattr(basis_publication, "_write", interrupt_after_ref)
    assert tio.enqueue_ticket("transaction") is False

    journal = basis_publication.load_basis_publication(root, "transaction")
    assert journal is not None
    prepared = journal.prepared["outer"]
    ticket_ref = journal.participants[0].ticket_ref
    assert _git(root, "rev-parse", ticket_ref) == prepared
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()

    monkeypatch.setattr(basis_publication, "_write", write_journal)
    assert tio.enqueue_ticket("transaction") is True
    assert basis_publication.load_basis_publication(root, "transaction") is None


def test_enqueue_retry_tolerates_retired_temporary_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project_dir, tio = _prepared_ticket(tmp_path)
    write_receipt = acceptance_basis_module.write_basis_receipt
    interrupted = False

    def interrupt_receipt(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("after basis commit publication")
        return write_receipt(*args, **kwargs)

    monkeypatch.setattr(acceptance_basis_module, "write_basis_receipt", interrupt_receipt)
    assert tio.enqueue_ticket("transaction") is False
    assert basis_publication.load_basis_publication(root, "transaction") is not None

    monkeypatch.setattr(acceptance_basis_module, "write_basis_receipt", write_receipt)
    assert tio.enqueue_ticket("transaction") is True
    assert basis_publication.load_basis_publication(root, "transaction") is None


def test_basis_publication_journal_rejects_boolean_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    monkeypatch.setattr(
        acceptance_basis_module,
        "write_basis_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("after basis publication")),
    )
    assert tio.enqueue_ticket("transaction") is False
    path = project_dir / ".runtime/acceptance/basis-publication/transaction.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema"] = True
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(basis_publication.BasisPublicationError, match="must be an integer"):
        basis_publication.load_basis_publication(root, "transaction")


def test_basis_receipt_rejects_boolean_schema(tmp_path: Path) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    assert tio.enqueue_ticket("transaction") is True
    queued = project_dir / "tickets/board/queue/transaction.md"
    fields, _body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    path = project_dir / f".runtime/acceptance/bases/transaction/{basis.basis_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema"] = True
    path.write_bytes(acceptance_basis_module.canonical_json(value))

    with pytest.raises(AcceptanceBasisError, match="receipt mismatch"):
        tio.load_basis("transaction")


def test_enqueue_retry_rejects_changed_effective_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    first_policy = {
        "destination": "review",
        "merge": True,
        "cleanup": True,
        "triage_report": True,
        "remove_targets": [],
    }
    second_policy = {**first_policy, "cleanup": False}

    monkeypatch.setattr(
        acceptance_basis_module,
        "write_basis_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("after basis publication")),
    )
    assert tio.enqueue_ticket("transaction", on_success=first_policy) is False

    assert tio.enqueue_ticket("transaction", on_success=second_policy) is False
    assert "effective Ticket fields changed" in capsys.readouterr().err
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()


def test_enqueue_retry_recovers_matching_orphan_record_without_staged_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    publish = basis_publication.publish_basis_commits
    interrupted = False

    def interrupt_before_journal(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("before publication journal")
        return publish(*args, **kwargs)

    monkeypatch.setattr(workspace_ops, "publish_basis_commits", interrupt_before_journal)
    assert tio.enqueue_ticket("transaction") is False
    assert basis_publication.load_basis_publication(root, "transaction") is None
    workspace = project_dir / "worktrees/transaction"
    assert _git(workspace, "diff", "--cached", "--name-only") == ""

    monkeypatch.setattr(workspace_ops, "publish_basis_commits", publish)
    assert tio.enqueue_ticket("transaction") is True


def test_paired_publication_rejects_changed_workspace_upstream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _root, project_dir, tio = _paired_basis_project(tmp_path)
    ticket = tio.create_ticket_file(
        "paired-routing",
        TicketFileSpec(
            summary="Pin paired routing",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    project_workspace = project_dir / "worktrees/paired-routing/.booley_project"
    _git(project_dir, "branch", "alternate")
    branch = _git(project_workspace, "branch", "--show-current")
    _git(project_workspace, "branch", "--set-upstream-to=alternate", branch)

    assert tio.enqueue_ticket("paired-routing") is False
    assert "upstream changed after authoring" in capsys.readouterr().err


def test_basis_reset_uses_preflighted_expected_head_cas(tmp_path: Path) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    assert tio.enqueue_ticket("transaction") is True
    queued = project_dir / "tickets/board/queue/transaction.md"
    fields, _body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    workspace = project_dir / "worktrees/transaction"
    (workspace / "implementation.txt").write_text("work\n", encoding="utf-8")
    _git(workspace, "add", "implementation.txt")
    _git(workspace, "commit", "-m", "implementation")
    implementation_sha = _git(workspace, "rev-parse", "HEAD")

    plan = workspace_ops.preflight_basis_reset(root, "transaction", basis, "main")
    assert plan.participants[0].expected_head == implementation_sha

    workspace_ops.reset_basis_worktrees(
        root,
        "transaction",
        basis,
        "main",
        plan=plan,
    )

    assert _git(root, "rev-parse", basis.participant("outer").ticket_ref) == basis.outer_sha
    assert _git(workspace, "rev-parse", "HEAD") == basis.outer_sha


def test_board_basis_rejects_stale_runtime_ticket_snapshot(tmp_path: Path) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    assert tio.enqueue_ticket("transaction")
    queued = project_dir / "tickets/board/queue/transaction.md"
    fields, body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    fields.pop("acceptance_basis")
    runtime_ticket = project_dir / "tickets/logs/transaction/ticket.md"
    runtime_ticket.parent.mkdir(parents=True, exist_ok=True)
    runtime_ticket.write_text(format_frontmatter(fields, body), encoding="utf-8")

    with pytest.raises(AcceptanceBasisError, match="acceptance_basis"):
        tio.load_basis("transaction", runtime_ticket_path=runtime_ticket)


def test_enqueue_recovery_rejects_advanced_ticket_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    prepare = tio._prepare_enqueue_publication

    def advance_ref(*args, **kwargs):
        journal = prepare(*args, **kwargs)
        worktree = project_dir / "worktrees/transaction"
        (worktree / "unexpected.txt").write_text("advanced\n", encoding="utf-8")
        _git(worktree, "add", "unexpected.txt")
        _git(worktree, "commit", "-m", "advance after prepare")
        return journal

    monkeypatch.setattr(tio, "_prepare_enqueue_publication", advance_ref)

    with pytest.raises(RuntimeError, match="moved after enqueue preparation"):
        tio.enqueue_ticket("transaction")
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()
    assert _git(root, "branch", "--show-current") == "main"


def test_enqueue_rejects_destination_movement_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)
    prepare = tio._prepare_enqueue_publication

    def advance_destination(*args, **kwargs):
        journal = prepare(*args, **kwargs)
        (root / "concurrent.txt").write_text("advanced\n", encoding="utf-8")
        _git(root, "add", "concurrent.txt")
        _git(root, "commit", "-m", "advance destination")
        return journal

    monkeypatch.setattr(tio, "_prepare_enqueue_publication", advance_destination)

    with pytest.raises(RuntimeError, match=r"destination ref .* moved after enqueue preparation"):
        tio.enqueue_ticket("transaction")
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()


def test_enqueue_rejects_stale_receipt_after_source_only_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    prepare = tio._prepare_enqueue_publication

    def interrupt(*_args, **_kwargs):
        raise OSError("after receipt")

    monkeypatch.setattr(tio, "_prepare_enqueue_publication", interrupt)
    with pytest.raises(OSError, match="after receipt"):
        tio.enqueue_ticket("transaction")

    draft = project_dir / "tickets/board/drafts/transaction.md"
    draft.write_text(
        draft.read_text(encoding="utf-8").replace(
            "summary: Recover publication", 'summary: "Recover publication"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tio, "_prepare_enqueue_publication", prepare)

    assert tio.enqueue_ticket("transaction") is False
    assert draft.exists()


def test_partial_keepalive_creation_rolls_forward_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    basis = AcceptanceBasis((_participant("outer"), _participant("project")))
    outer = tmp_path / "outer"
    project = tmp_path / "project"
    refs: dict[tuple[Path, str], str] = {}
    fail_project = True

    def fake_git(repository: Path, *args: str):
        ref = args[-1].removesuffix("^{commit}")
        value = refs.get((repository, ref))
        return subprocess.CompletedProcess(
            args, 0 if value else 1, f"{value}\n" if value else "", ""
        )

    def fake_require(repository: Path, *args: str) -> str:
        nonlocal fail_project
        ref = args[1]
        if repository == project and fail_project:
            fail_project = False
            raise basis_publication.BasisPublicationError("project ref failed")
        refs[(repository, ref)] = args[2]
        return ""

    monkeypatch.setattr(basis_publication, "_git", fake_git)
    monkeypatch.setattr(basis_publication, "_require_git", fake_require)

    repositories = {"outer": outer, "project": project}
    with pytest.raises(basis_publication.BasisPublicationError, match="project ref failed"):
        basis_publication._publish_basis_keepalives(repositories, basis)

    outer_ref = f"refs/booley/bases/{basis.basis_id}/outer"
    assert refs[(outer, outer_ref)] == basis.participant("outer").authoring_sha

    basis_publication._publish_basis_keepalives(repositories, basis)

    project_ref = f"refs/booley/bases/{basis.basis_id}/project"
    assert refs[(project, project_ref)] == basis.participant("project").authoring_sha


def test_malformed_committed_record_fails_with_basis_error(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    record = root / ".booley_project/acceptance/bases/malformed.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"bindings":[],"schema":1,"ticket":{}}\n', encoding="utf-8")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "malformed record")
    sha = _git(root, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                sha,
                "refs/heads/booley-generation/0123456789abcdef/malformed",
                "refs/heads/main",
                sha,
            ),
        )
    )

    with pytest.raises(AcceptanceBasisError, match=r"record\.ticket"):
        load_basis_record(root, "malformed", basis)


def test_protected_parent_symlink_change_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    hook = root / ".booley_project/hooks/run.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("print('baseline')\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "protected hook")
    sha = _git(root, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                sha,
                "refs/heads/booley-generation/0123456789abcdef/symlink",
                "refs/heads/main",
                sha,
            ),
        )
    )
    hook.unlink()
    hook.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".booley_project/hooks").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AcceptanceBasisError, match="protected path"):
        assert_inputs_unchanged(basis, root)


def test_acceptance_path_policy_protects_routing_config(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = root / ".booley_project"
    project.mkdir(parents=True)
    (root / "booley.toml").write_text('[project]\ndir = ".booley_project"\n', encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "booley.toml")
    _git(root, "commit", "-m", "route project")
    sha = _git(root, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                sha,
                "refs/heads/booley-generation/0123456789abcdef/routing",
                "refs/heads/main",
                sha,
            ),
        )
    )
    (root / "booley.toml").write_text('[project]\ndir = "other"\n', encoding="utf-8")

    with pytest.raises(AcceptanceBasisError, match="protected path"):
        assert_inputs_unchanged(basis, root)


def test_gitignored_untracked_control_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".gitignore").write_text("/.booley_project/\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "baseline")
    sha = _git(root, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                sha,
                "refs/heads/booley-generation/0123456789abcdef/ignored-control",
                "refs/heads/main",
                sha,
            ),
        )
    )
    hook = root / ".booley_project/hooks/pre-run.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("print('changed')\n", encoding="utf-8")

    with pytest.raises(AcceptanceBasisError, match="protected path"):
        assert_inputs_unchanged(basis, root)


def test_protected_input_git_discovery_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = subprocess.CompletedProcess(
        ["git", "ls-files"],
        returncode=128,
        stdout="",
        stderr="broken index",
    )
    monkeypatch.setattr(acceptance_targets.subprocess, "run", lambda *_args, **_kwargs: failed)

    with pytest.raises(AcceptanceBasisError, match="protected-input discovery failed"):
        AcceptancePathPolicy().discover(tmp_path)


def test_referenced_program_paths_include_redirecting_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    real = root / "real-hooks"
    real.mkdir(parents=True)
    (real / "run.py").write_text("print('run')\n", encoding="utf-8")
    (root / "hooks").symlink_to(real, target_is_directory=True)

    paths = referenced_program_paths(
        {"pre_run": "hooks/run.py"},
        search_roots=(root,),
        project_root=root,
        strict=True,
    )

    assert root / "hooks" in paths
    assert real / "run.py" in paths


def test_binding_selector_validation_rejects_changed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = AcceptanceTargetBinding(
        "sim",
        "criteria.mandatory.sim_pass",
        "acme:lib:old#sim",
        "acme:lib:old#sim",
        "sim",
        "sim",
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_targets.select_target",
        lambda *_args, **_kwargs: SimpleNamespace(identity="acme:lib:new#sim"),
    )

    errors = validate_binding_selectors(tmp_path, (binding,))

    assert errors and "expected 'acme:lib:old#sim'" in errors[0]


def test_return_to_draft_journal_rejects_noncanonical_paths(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    journal = draft_transition._new_journal(
        root, blocked, "blocked-again", "blocked", tio.logs_dir
    )
    impostor = tmp_path / "other" / "drafts" / "blocked-again.md"

    with pytest.raises(draft_transition.DraftTransitionError, match="destination path"):
        draft_transition._validate_journal(
            root,
            tio.logs_dir,
            "blocked-again",
            replace(journal, draft_ticket=str(impostor)),
        )


def test_return_to_draft_rejects_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    lock = tio.logs_dir / "blocked-again/.runtime/ticket.lock"
    lock.write_text("424242", encoding="utf-8")
    monkeypatch.setattr("booley.runtime.pid.is_pid_alive", lambda _pid: True)

    with pytest.raises(RuntimeError, match="owned by live process"):
        tio.return_to_draft("blocked-again")


def test_return_to_draft_rejects_callers_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    lock = tio.logs_dir / "blocked-again/.runtime/ticket.lock"
    lock.write_text(str(tio._resolve_developer_pid()), encoding="utf-8")
    monkeypatch.setattr("booley.runtime.pid.is_pid_alive", lambda _pid: True)

    with pytest.raises(RuntimeError, match="owned by live process"):
        tio.return_to_draft("blocked-again")


def test_return_to_draft_rejects_jobs_and_acceptance_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    monkeypatch.setattr(
        "booley.harness.job_fence.active_ticket_jobs",
        lambda _path: [SimpleNamespace(endpoint="sim", run_id="job-1")],
    )
    with pytest.raises(RuntimeError, match="active endpoint Jobs"):
        tio.return_to_draft("blocked-again")

    monkeypatch.setattr("booley.harness.job_fence.active_ticket_jobs", lambda _path: [])
    monkeypatch.setattr(
        "booley.ticket_board.io.acceptance_state",
        lambda _tickets, _slug: JournalState.PREPARED,
    )
    with pytest.raises(RuntimeError, match="publication in progress"):
        tio.return_to_draft("blocked-again")


def test_return_to_draft_recovers_before_board_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board import draft_transition

    root, blocked, tio = _blocked_ticket(tmp_path)
    old_worktree = root / ".booley_project/worktrees/blocked-again"
    (old_worktree / "unfinished.txt").write_text("preserve me\n", encoding="utf-8")
    old_log = tio.logs_dir / "blocked-again/human-logs/run.log"
    old_log.parent.mkdir(parents=True, exist_ok=True)
    old_log.write_text("old evidence\n", encoding="utf-8")
    publish_generation = draft_transition._publish_generation
    interrupted = False

    def interrupt(*args, **kwargs):
        nonlocal interrupted
        publish_generation(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("after descriptor cutover")

    monkeypatch.setattr(draft_transition, "_publish_generation", interrupt)
    with pytest.raises(OSError, match="descriptor cutover"):
        tio.return_to_draft("blocked-again")
    assert blocked.exists()
    monkeypatch.setattr(draft_transition, "_publish_generation", publish_generation)

    reopened = tio.return_to_draft("blocked-again")

    assert Path(reopened["outer_worktree"]).is_dir()
    old_paths = _git(root, "worktree", "list", "--porcelain")
    archived_outer = next(
        Path(line.removeprefix("worktree "))
        for line in old_paths.splitlines()
        if line.startswith("worktree ") and "old-outer" in line
    )
    assert (archived_outer / "unfinished.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert (tio.logs_dir / "blocked-again/runs/001/human-logs/run.log").exists()
