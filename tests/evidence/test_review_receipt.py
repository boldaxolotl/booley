"""Atomic Reviewer receipt and source-scoped freshness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewContextError,
    ReviewInvocation,
    build_review_contract_detail,
    finalize_review_detail,
    review_invocation_changed,
    review_receipt_drift,
)


def _detail(tmp_path: Path, monkeypatch, *, spec_path: Path | None = None) -> dict:
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl/uart.sv").write_text("module uart; endmodule\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="clean",
            spec_path=spec_path,
            steering="prefer reset behavior",
        )
    )
    return {"review_detail_version": REVIEW_DETAIL_VERSION, "contract": contract}


def test_receipt_id_covers_contract_and_source(tmp_path: Path, monkeypatch) -> None:
    detail = _detail(tmp_path, monkeypatch)

    first = finalize_review_detail(detail, {"categories": ["rtl"], "fingerprint": {"a": 1}})
    second = finalize_review_detail(detail, {"categories": ["rtl"], "fingerprint": {"a": 2}})
    changed_contract = {**detail["contract"], "steering_digest": "changed"}
    third = finalize_review_detail(
        {**detail, "contract": changed_contract},
        {"categories": ["rtl"], "fingerprint": {"a": 1}},
    )

    assert len({first["receipt_id"], second["receipt_id"], third["receipt_id"]}) == 3


def test_only_scoped_source_edits_stale_receipt(tmp_path: Path, monkeypatch) -> None:
    detail = _detail(tmp_path, monkeypatch)
    assert review_receipt_drift(detail, tmp_path) == []

    (tmp_path / "rtl/unrelated.sv").write_text("module other; endmodule\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == []

    (tmp_path / "rtl/uart.sv").write_text("module uart; logic x; endmodule\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == ["scope"]


def test_scoped_source_edits_preserve_invocation_identity(tmp_path: Path, monkeypatch) -> None:
    detail = _detail(tmp_path, monkeypatch)
    previous = detail["contract"]
    (tmp_path / "rtl/uart.sv").write_text("module uart; logic x; endmodule\n", encoding="utf-8")
    current = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="clean",
            steering="prefer reset behavior",
        )
    )

    assert previous["scope_hashes"] != current["scope_hashes"]
    assert review_invocation_changed(previous, current) is False

    changed_scope = {**current, "scope": ["rtl/other.sv"]}
    assert review_invocation_changed(previous, changed_scope) is True


def test_ticket_mode_context_is_authoritative(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticket.md").write_text("Implement UART registers.\n", encoding="utf-8")
    (logs / "answered_questions.md").write_text("Use active-low reset.\n", encoding="utf-8")
    explicit = tmp_path / "explicit.md"
    explicit.write_text("This must not override Ticket Mode.\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="spec",
            scope=("rtl/uart.sv",),
            mode="done",
            spec_path=explicit,
        )
    )
    detail = {"review_detail_version": REVIEW_DETAIL_VERSION, "contract": contract}

    assert contract["ticket_source"] == str((logs / "ticket.md").resolve())
    assert contract["spec_source"] == ""
    assert review_receipt_drift(detail, tmp_path) == []
    (logs / "answered_questions.md").write_text("Use synchronous reset.\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == ["decisions"]


def test_ticket_linked_spec_is_resolved_without_ticket_board_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("Latency is three cycles.\n", encoding="utf-8")
    (logs / "ticket.md").write_text(
        "---\nspec: spec.md\n---\nImplement the design.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))

    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="spec",
            scope=(),
            mode="done",
        )
    )

    assert contract["spec_source"] == str(spec.resolve())
    assert contract["spec_digest"]


def test_malformed_ticket_frontmatter_raises_review_context_error(
    tmp_path: Path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticket.md").write_text("---\nspec: [unterminated\n---\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))

    with pytest.raises(ReviewContextError, match="frontmatter"):
        build_review_contract_detail(
            ReviewInvocation(
                work_dir=tmp_path,
                category="rtl",
                focus="spec",
                scope=(),
                mode="done",
            )
        )


def test_unreadable_ticket_frontmatter_raises_review_context_error(
    tmp_path: Path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    ticket = logs / "ticket.md"
    ticket.write_text("---\nspec: spec.md\n---\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    original_read_text = Path.read_text

    def fail_ticket_read(path: Path, *args, **kwargs) -> str:
        if path == ticket:
            raise OSError("synthetic read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_ticket_read)

    with pytest.raises(ReviewContextError, match="frontmatter"):
        build_review_contract_detail(
            ReviewInvocation(
                work_dir=tmp_path,
                category="rtl",
                focus="spec",
                scope=(),
                mode="done",
            )
        )


def test_ticket_board_policy_digest_controls_receipt_freshness(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "rtl").mkdir()
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="tb",
            focus="bugs",
            scope=(),
            mode="clean",
            tb_policy_digest="policy-a",
        )
    )
    detail = {"review_detail_version": REVIEW_DETAIL_VERSION, "contract": contract}

    assert review_receipt_drift(detail, tmp_path, tb_policy_digest="policy-a") == []
    assert review_receipt_drift(detail, tmp_path, tb_policy_digest="policy-b") == ["tb_policy"]


def test_explicit_spec_is_tracked_in_standalone_mode(tmp_path: Path, monkeypatch) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("Latency is three cycles.\n", encoding="utf-8")
    detail = _detail(tmp_path, monkeypatch, spec_path=spec)

    assert review_receipt_drift(detail, tmp_path) == []
    spec.write_text("Latency is four cycles.\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == ["spec"]


def test_missing_persisted_spec_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("Latency is three cycles.\n", encoding="utf-8")
    detail = _detail(tmp_path, monkeypatch, spec_path=spec)
    spec.unlink()

    with pytest.raises(ReviewContextError, match="context"):
        review_receipt_drift(detail, tmp_path)


def test_pre_v4_receipt_uses_legacy_source_fingerprint_path(tmp_path: Path, monkeypatch) -> None:
    detail = _detail(tmp_path, monkeypatch)
    detail["review_detail_version"] = 3

    assert review_receipt_drift(detail, tmp_path) == []
