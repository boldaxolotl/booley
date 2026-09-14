"""Real-Git proof for a metadata-only amendment joined to saved implementation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
    authored_ticket_record_from_spec,
    canonical_json,
    load_basis_record,
    materialize_basis_checkout,
    materialize_ticket_commits,
    validate_current_basis_refs,
)
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
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


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(path: Path, *, record_path: str) -> tuple[Path, str, str, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Booley Test")
    _git(path, "config", "user.email", "test@booley.invalid")
    (path / "rtl").mkdir()
    (path / "rtl/design.sv").write_text("module baseline; endmodule\n")
    baseline = _commit(path, "baseline")
    _git(path, "checkout", "-b", "authoring")
    _write_record(path, record_path, 500)
    _git(path, "add", "-f", record_path)
    authoring = _commit(path, "authoring")
    ref = "refs/heads/booley-generation/0123456789abcdef/ticket"
    _git(path, "branch", ref.removeprefix("refs/heads/"), authoring)
    checkout = path.parent / f"{path.name}-execution"
    _git(path, "worktree", "add", str(checkout), ref.removeprefix("refs/heads/"))
    (checkout / "rtl/design.sv").write_text("module implemented; endmodule\n")
    execution = _commit(checkout, "implementation")
    return checkout, baseline, authoring, execution


def _write_record(repository: Path, path: str, floor: int) -> None:
    ticket = (
        "---\nsummary: timing\ntype: feature\nbranch: main\n"
        "scope: [rtl/design.sv]\non_success: [review, merge]\n"
        f"CRITERIA_MANDATORY: {{SYNTH: {{synth: {{fmax_mhz_min: {floor}}}}}}}\n"
        "---\n\n## Description\n\nTiming requirement.\n"
    )
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: (),
    )
    conversion = convert_ticket_document(
        ticket, TicketConversionContext("draft", lambda _generated: view)
    )
    assert conversion.document is not None, conversion.diagnostics
    record = authored_ticket_record_from_spec(conversion.document.spec, ())
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json(record))


def _amend(
    owner: Path, execution: Path, record_path: str, previous_authoring: str, floor: int
) -> tuple[str, str]:
    authoring_checkout = owner.parent / f"{owner.name}-authoring"
    if authoring_checkout.exists():
        _git(authoring_checkout, "checkout", "--detach", previous_authoring)
    else:
        _git(owner, "worktree", "add", "--detach", str(authoring_checkout), previous_authoring)
    _write_record(authoring_checkout, record_path, floor)
    new_authoring = _commit(authoring_checkout, f"amend to {floor}")
    old_execution = _git(execution, "rev-parse", "HEAD")
    _write_record(execution, record_path, floor)
    _git(execution, "add", "-f", record_path)
    tree = _git(execution, "write-tree")
    joined = _git(
        execution,
        "commit-tree",
        tree,
        "-p",
        old_execution,
        "-p",
        new_authoring,
        "-m",
        f"join amendment {floor}",
    )
    _git(execution, "reset", "--hard", joined)
    assert _git(execution, "diff", "--name-only", old_execution, joined) == record_path
    assert _git(execution, "merge-base", "--is-ancestor", new_authoring, joined) == ""
    return new_authoring, joined


@pytest.mark.parametrize("paired", [False, True])
def test_repeated_amendment_retains_basis_and_implementation(tmp_path: Path, paired: bool) -> None:
    root = tmp_path / "outer"
    outer_record = ".booley_project/acceptance/bases/ticket.json"
    outer_checkout, outer_baseline, outer_authoring, outer_execution = _repository(
        root, record_path="README.md" if paired else outer_record
    )
    owners = {"outer": (root, outer_checkout, outer_baseline, outer_authoring, outer_execution)}
    if paired:
        project = root / ".booley_project"
        project_checkout, project_baseline, project_authoring, project_execution = _repository(
            project, record_path="acceptance/bases/ticket.json"
        )
        owners["project"] = (
            project,
            project_checkout,
            project_baseline,
            project_authoring,
            project_execution,
        )
        # The outer Ticket checkout must expose the paired repository at its standard path.
        _git(
            project,
            "worktree",
            "move",
            str(project_checkout),
            str(outer_checkout / ".booley_project"),
        )
        owners["project"] = (
            project,
            outer_checkout / ".booley_project",
            project_baseline,
            project_authoring,
            project_execution,
        )
    previous = {role: values[3] for role, values in owners.items()}
    for floor in (430, 420):
        heads = {}
        for role, (owner, checkout, _baseline, _authoring, _execution) in owners.items():
            path = "acceptance/bases/ticket.json" if role == "project" else outer_record
            if role == "outer" and paired:
                continue
            previous[role], heads[role] = _amend(owner, checkout, path, previous[role], floor)
        if paired:
            owner, checkout, *_ = owners["outer"]
            # The outer participant has no requirements record in the paired layout.
            heads["outer"] = _git(checkout, "rev-parse", "HEAD")
        participants = tuple(
            BasisParticipant(
                role,
                previous[role],
                "refs/heads/booley-generation/0123456789abcdef/ticket",
                "refs/heads/main",
                values[2],
            )
            for role, values in sorted(owners.items())
        )
        basis = AcceptanceBasis(participants)
        [criterion] = load_basis_record(root, "ticket", basis)["ticket"]["spec"]["criteria"]
        assert criterion["capability"] == "SYNTH"
        assert criterion["value"]["threshold"] == floor
        assert validate_current_basis_refs(root, basis) == heads
        materialize_basis_checkout(root, basis, tmp_path / f"basis-{floor}")
        materialize_ticket_commits(root, basis, tmp_path / f"current-{floor}", heads)
        for role, (owner, checkout, baseline, _authoring, _execution) in owners.items():
            assert _git(checkout, "diff", "--name-only", baseline, heads[role]).splitlines()
            assert _git(checkout, "show", f"{heads[role]}:rtl/design.sv") == (
                "module implemented; endmodule"
            )
            assert _git(owner, "rev-parse", "main") == baseline
        # Reset is intentionally explicit: its target is the latest authoring tree.
        assert all(
            _git(values[0], "show", f"{previous[role]}:rtl/design.sv")
            == "module baseline; endmodule"
            for role, values in owners.items()
        )
