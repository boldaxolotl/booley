"""Acceptance Basis schema and hard-cutoff behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    authored_ticket_record,
    load_acceptance_basis,
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


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"schema": 2, "participants": []}, "schema must be 1"),
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
    assert record["ticket"] == {"frontmatter": fields, "body": "exact body"}


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
        check=True,
    )
    return result.stdout.strip()


def test_enqueue_automatically_publishes_basis_record_and_receipt(tmp_path: Path) -> None:
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
    tio = TicketIO(project_dir / "tickets", project_root=root)

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


def test_return_to_draft_preserves_old_ref_and_allocates_new_generation(
    tmp_path: Path,
) -> None:
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
    tio = TicketIO(project_dir / "tickets", project_root=root)
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
