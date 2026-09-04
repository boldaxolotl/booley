"""Regression coverage for issue #88 / F-43 control-artifact round trips."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from booley.harness._ticket_ops import DirectTicketOps
from booley.ticket_board.acceptance_basis import AcceptanceBasis
from booley.ticket_board.cli import main
from booley.ticket_board.criteria_markdown import (
    parse_criteria_section,
    render_criteria_section,
)
from booley.ticket_board.frontmatter import parse_frontmatter, update_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.operations import op_complete
from booley.ticket_board.scanner import find_ticket_file
from booley.ticket_board.validation import validate_ticket_fields


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _project(tmp_path: Path, monkeypatch) -> tuple[Path, TicketIO]:
    root = tmp_path / "rtl"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "rtl").mkdir()
    (root / "rtl" / "toy.sv").write_text("module toy; endmodule\n", encoding="utf-8")
    (root / "toy.core").write_text(
        "\n".join(
            [
                "CAPI=2:",
                "name: acme:lib:toy:1.0",
                "filesets:",
                "  rtl:",
                "    files: [rtl/toy.sv]",
                "    file_type: systemVerilogSource",
                "targets:",
                "  lint_toy:",
                "    flow: lint",
                "    flow_options: {tool: verilator}",
                "    filesets: [rtl]",
                "    toplevel: toy",
                "",
            ]
        ),
        encoding="utf-8",
    )
    project = root / ".booley_project"
    (project / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project / "booley.toml").write_text(
        "[flows.lint]\ndefault_target = 'lint_toy'\n", encoding="utf-8"
    )
    (project / "tests.toml").write_text("[lint_toy]\ntests = ['baseline']\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    _git(
        root,
        "add",
        "-f",
        ".booley_project/.gitignore",
        ".booley_project/booley.toml",
        ".booley_project/tests.toml",
    )
    _commit_all(root, "initial project")
    return root, TicketIO(project / "tickets", project_root=root)


def _ticket(tio: TicketIO, *, merge: bool = False) -> Path:
    path = tio.create_ticket_file(
        "change-target",
        TicketFileSpec(
            summary="Change the Target contract",
            ticket_type="refactor",
            branch="main",
            scope=["toy.core"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nExercise every Booley-owned control artifact.\n",
        ),
    )
    assert path is not None
    update_frontmatter(
        path,
        {"on_success": {"destination": "review", "merge": merge, "cleanup": False}},
    )
    return path


def test_authored_draft_validates_without_hiding_product_changes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, tio = _project(tmp_path, monkeypatch)
    ticket = _ticket(tio)
    _git(root, "add", "-f", str(ticket.relative_to(root)))
    _commit_all(root, "add draft ticket")
    update_frontmatter(ticket, {"priority": "high"})
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    monkeypatch.setenv("TICKETS_DIR", str(tio.tickets_dir))
    capsys.readouterr()

    assert main(["validate-ticket", str(ticket), "--check-git"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "errors": [],
        "valid": True,
        "warnings": [],
    }
    assert DirectTicketOps().validate_ticket(root, str(ticket), check_git=True) == {
        "errors": [],
        "valid": True,
    }

    (root / "rtl" / "toy.sv").write_text(
        "module toy; wire unrelated_product_edit; endmodule\n", encoding="utf-8"
    )
    assert main(["validate-ticket", str(ticket), "--check-git"]) == 1
    assert any(
        "Dirty working tree" in error for error in json.loads(capsys.readouterr().out)["errors"]
    )
    assert any(
        "Dirty working tree" in error
        for error in DirectTicketOps().validate_ticket(root, str(ticket), check_git=True)["errors"]
    )


def test_validate_ticket_does_not_exempt_a_product_markdown_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, tio = _project(tmp_path, monkeypatch)
    product_ticket = root / "ticket-shaped-product-file.md"
    _ticket(tio).rename(product_ticket)
    _commit_all(root, "add product markdown")
    update_frontmatter(product_ticket, {"priority": "high"})
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    monkeypatch.setenv("TICKETS_DIR", str(tio.tickets_dir))
    capsys.readouterr()

    assert main(["validate-ticket", str(product_ticket), "--check-git"]) == 1
    assert any(
        "Dirty working tree" in error for error in json.loads(capsys.readouterr().out)["errors"]
    )
    assert any(
        "Dirty working tree" in error
        for error in DirectTicketOps().validate_ticket(
            root,
            str(product_ticket),
            check_git=True,
        )["errors"]
    )


def test_ticket_validation_normalizes_a_draft_path_from_a_project_subdirectory(
    tmp_path: Path, monkeypatch
) -> None:
    root, tio = _project(tmp_path, monkeypatch)
    ticket = _ticket(tio)
    _git(root, "add", "-f", str(ticket.relative_to(root)))
    _commit_all(root, "add draft ticket")
    update_frontmatter(ticket, {"priority": "high"})
    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    workdir = root / "rtl"
    monkeypatch.chdir(workdir)

    errors = validate_ticket_fields(
        fields,
        body,
        check_files=True,
        check_git=True,
        project_root=root,
        allowed_dirty_paths=(Path("..") / ticket.relative_to(root),),
    )

    assert not any("Dirty working tree" in error for error in errors)


def test_enqueue_records_tests_toml_update_in_acceptance_basis(
    tmp_path: Path, monkeypatch
) -> None:
    root, tio = _project(tmp_path, monkeypatch)
    _ticket(tio)
    outer = root / ".booley_project" / "worktrees" / "change-target"
    tests_toml = outer / ".booley_project" / "tests.toml"
    tests_toml.write_text("[lint_toy]\ntests = ['smoke']\n", encoding="utf-8")

    assert tio.enqueue_ticket("change-target") is True
    queue = tio.tickets_dir / "board" / "queue" / "change-target.md"
    fields, _body = parse_frontmatter(queue.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    outer_participant = basis.participant("outer")

    assert outer_participant.authoring_sha == _git(outer, "rev-parse", "HEAD")
    assert (
        _git(outer, "show", "HEAD:.booley_project/tests.toml")
        == tests_toml.read_text(encoding="utf-8").strip()
    )
    assert _git(root, "rev-parse", outer_participant.ticket_ref) == outer_participant.authoring_sha


def test_mutation_campaign_dictionary_round_trips_through_markdown() -> None:
    criteria = {
        "mandatory": {
            "mutation_score": [{"target": "sim_toy", "scope": ["rtl/toy.sv"], "min_score": 80}]
        }
    }

    assert parse_criteria_section(render_criteria_section(criteria)) == criteria


def test_json_shaped_criterion_string_round_trips_as_a_string() -> None:
    criteria = {"mandatory": {"sim_pass": ['{"target":"literal"}']}}

    assert parse_criteria_section(render_criteria_section(criteria)) == criteria


def test_review_completion_ignores_its_board_rename_but_not_product_edits(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "booley.ticket_board.operations._completion_acceptance_valid", lambda *_: True
    )
    root, tio = _project(tmp_path, monkeypatch)
    draft = _ticket(tio, merge=True)
    worktree = root / ".booley_project" / "worktrees" / "change-target"
    core = worktree / "toy.core"
    core.write_text(core.read_text(encoding="utf-8").replace(":1.0", ":2.0"), encoding="utf-8")
    assert tio.enqueue_ticket("change-target") is True
    queue = draft.parent.parent / "queue" / draft.name
    unrelated_ticket = queue.parent / "unrelated-ticket.md"
    unrelated_ticket.write_text(queue.read_text(encoding="utf-8"), encoding="utf-8")
    _git(
        root,
        "add",
        "-f",
        str(queue.relative_to(root)),
        str(unrelated_ticket.relative_to(root)),
    )
    _commit_all(root, "queue ticket")

    review = queue.parent.parent / "review" / queue.name
    review.parent.mkdir(parents=True, exist_ok=True)
    queue.rename(review)
    monkeypatch.chdir(root)

    source = root / "rtl" / "toy.sv"
    original = source.read_text(encoding="utf-8")
    source.write_text("module toy; wire unrelated_product_edit; endmodule\n", encoding="utf-8")
    assert op_complete(tio, "change-target") is False
    assert find_ticket_file(tio.tickets_dir, "change-target")[1] == "review"

    source.write_text(original, encoding="utf-8")
    unrelated_original = unrelated_ticket.read_text(encoding="utf-8")
    update_frontmatter(unrelated_ticket, {"priority": "high"})
    assert op_complete(tio, "change-target") is False
    assert find_ticket_file(tio.tickets_dir, "change-target")[1] == "review"

    unrelated_ticket.write_text(unrelated_original, encoding="utf-8")
    assert op_complete(tio, "change-target") is True
    assert find_ticket_file(tio.tickets_dir, "change-target")[1] == "done"
    assert "acme:lib:toy:2.0" in (root / "toy.core").read_text(encoding="utf-8")
