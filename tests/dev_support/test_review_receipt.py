"""Atomic Reviewer receipt and non-source freshness tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.dev_support.review_receipt import (
    ReviewInvocation,
    ReviewTicketError,
    build_review_contract_detail,
    finalize_review_detail,
    review_receipt_drift,
)


def _contract(tmp_path: Path, monkeypatch) -> dict:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "ticket.md").write_text("Current: implement UART registers.\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    return build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="clean",
            targets=(),
            target_kind="none",
        )
    )


def test_receipt_id_covers_contract_and_source(tmp_path: Path, monkeypatch) -> None:
    contract = _contract(tmp_path, monkeypatch)
    draft = {"review_detail_version": 3, "contract": contract}

    first = finalize_review_detail(draft, {"categories": ["rtl"], "fingerprint": {"a": 1}})
    second = finalize_review_detail(draft, {"categories": ["rtl"], "fingerprint": {"a": 2}})
    changed_contract = {**contract, "scope": ["rtl/other.sv"]}
    third = finalize_review_detail(
        {**draft, "contract": changed_contract},
        {"categories": ["rtl"], "fingerprint": {"a": 1}},
    )

    assert len({first["receipt_id"], second["receipt_id"], third["receipt_id"]}) == 3
    assert first["_source_fingerprint"]["fingerprint"] == {"a": 1}


def test_ticket_and_target_edits_stale_receipt(tmp_path: Path, monkeypatch) -> None:
    contract = _contract(tmp_path, monkeypatch)
    detail = {"contract": contract}
    logs = Path(os.environ["BOOLEY_LOGS_DIR"])

    assert review_receipt_drift(detail, tmp_path) == []

    (logs / "ticket.md").write_text("Current: changed UART contract.\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == ["ticket"]

    (tmp_path / "uart.core").write_text(
        "CAPI=2:\nname: acme:uart:uart:1\ntargets:\n  lint: {flow: lint}\n",
        encoding="utf-8",
    )
    assert review_receipt_drift(detail, tmp_path) == ["ticket", "target_surface"]


def test_explicit_ticket_source_is_reused_without_logs_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ticket = tmp_path / "interactive-ticket.md"
    ticket.write_text("Implement UART registers.\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="done",
            targets=(),
            target_kind="none",
            ticket_path=ticket,
        )
    )

    assert review_receipt_drift({"contract": contract}, tmp_path) == []
    ticket.write_text("Implement changed UART registers.\n", encoding="utf-8")
    assert review_receipt_drift({"contract": contract}, tmp_path) == ["ticket"]


def test_missing_binding_ticket_fails_loud(tmp_path: Path, monkeypatch) -> None:
    ticket = tmp_path / "interactive-ticket.md"
    ticket.write_text("Implement UART registers.\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="done",
            targets=(),
            target_kind="none",
            ticket_path=ticket,
        )
    )
    ticket.unlink()

    with pytest.raises(ReviewTicketError, match="Ticket context"):
        review_receipt_drift({"contract": contract}, tmp_path)
