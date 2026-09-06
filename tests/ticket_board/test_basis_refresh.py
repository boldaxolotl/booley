"""Basis Refresh reconstructs only approved consumer-authored controls."""

from pathlib import Path
from types import SimpleNamespace

import yaml

from booley.core.models import TargetPlan, TargetPlanRole
from booley.ticket_board import basis_refresh
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
    ProviderTargetBinding,
)
from booley.ticket_board.basis_refresh import (
    _reapply_placeholders,
    _reapply_targets,
    _verify_providers,
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
