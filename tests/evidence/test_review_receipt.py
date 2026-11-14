"""Atomic Reviewer receipt and source-scoped freshness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewContextError,
    ReviewInvocation,
    build_review_contract_detail,
    current_review_receipt_identity,
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


def _ticket_detail(
    tmp_path: Path,
    monkeypatch,
    *,
    decisions: str | None,
) -> tuple[dict, Path]:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticket.md").write_text("Implement UART registers.\n", encoding="utf-8")
    decisions_path = logs / "answered_questions.md"
    if decisions is not None:
        decisions_path.write_text(decisions, encoding="utf-8")
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
    return {"review_detail_version": REVIEW_DETAIL_VERSION, "contract": contract}, decisions_path


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


def test_persisted_context_and_explicit_spec_are_tracked(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticket.md").write_text("Implement UART registers.\n", encoding="utf-8")
    (logs / "answered_questions.md").write_text("Use active-low reset.\n", encoding="utf-8")
    explicit = tmp_path / "explicit.md"
    explicit.write_text("Explicit review specification.\n", encoding="utf-8")
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
    assert contract["spec_source"] == str(explicit.resolve())
    assert review_receipt_drift(detail, tmp_path) == []
    (logs / "answered_questions.md").write_text("Use synchronous reset.\n", encoding="utf-8")
    assert review_receipt_drift(detail, tmp_path) == []


def test_decisions_creation_does_not_change_receipt(tmp_path: Path, monkeypatch) -> None:
    detail, decisions = _ticket_detail(tmp_path, monkeypatch, decisions=None)

    assert review_receipt_drift(detail, tmp_path) == []
    decisions.write_bytes(b"")
    assert review_receipt_drift(detail, tmp_path) == []


def test_decisions_removal_does_not_change_receipt(tmp_path: Path, monkeypatch) -> None:
    detail, decisions = _ticket_detail(
        tmp_path,
        monkeypatch,
        decisions="Use active-low reset.\n",
    )
    decisions.unlink()

    assert review_receipt_drift(detail, tmp_path) == []


def test_decisions_are_never_read(tmp_path: Path, monkeypatch) -> None:
    detail, decisions = _ticket_detail(
        tmp_path,
        monkeypatch,
        decisions="Use active-low reset.\n",
    )
    original_read_bytes = Path.read_bytes

    def fail_decisions_read(path: Path) -> bytes:
        if path == decisions:
            raise OSError("synthetic read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_decisions_read)

    assert review_receipt_drift(detail, tmp_path) == []
    assert not any(key.startswith("decisions_") for key in detail["contract"])


def test_explicit_spec_is_tracked_in_review_receipt(tmp_path: Path, monkeypatch) -> None:
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
            spec_path=spec,
        )
    )

    assert contract["spec_source"] == str(spec.resolve())
    assert contract["spec_digest"]


def test_review_policy_digest_controls_receipt_freshness(tmp_path: Path, monkeypatch) -> None:
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
    # A v4 receipt would report "scope" drift here; a pre-v4 one must stay current.
    (tmp_path / "rtl/uart.sv").write_text("module uart; logic x; endmodule\n", encoding="utf-8")

    assert review_receipt_drift(detail, tmp_path) == []


def _guide_detail(tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
    """A receipt recording one guide that lives in a temporary package-data directory."""
    from booley.specialists.reviewer import REVIEW_GUIDES, ReviewGuide

    package_data = tmp_path / "package-data"
    guide_file = package_data / "refs/code_review/rtl/bugs-checklist.md"
    guide_file.parent.mkdir(parents=True)
    guide_file.write_text("- check resets\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("booley.runtime.paths.package_data_dir", lambda: package_data)
    guide = ReviewGuide("checklist", "rtl/bugs-checklist.md")
    monkeypatch.setitem(REVIEW_GUIDES, ("rtl", "bugs"), (guide,))
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl/uart.sv").write_text("module uart; endmodule\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    contract = build_review_contract_detail(
        ReviewInvocation(
            work_dir=tmp_path,
            category="rtl",
            focus="bugs",
            scope=("rtl/uart.sv",),
            mode="done",
            guides=(guide,),
        )
    )
    return {"review_detail_version": REVIEW_DETAIL_VERSION, "contract": contract}, guide_file


def test_receipt_without_guide_digests_is_stale(tmp_path: Path, monkeypatch) -> None:
    """A released receipt records no guides, so its guide content is unknown."""
    detail, _ = _guide_detail(tmp_path, monkeypatch)
    assert review_receipt_drift(detail, tmp_path) == []
    del detail["contract"]["guides"]
    assert review_receipt_drift(detail, tmp_path) == ["guides"]


def test_guide_edit_or_deletion_stales_the_receipt(tmp_path: Path, monkeypatch) -> None:
    detail, guide_file = _guide_detail(tmp_path, monkeypatch)
    identity = current_review_receipt_identity(detail, tmp_path)
    guide_file.write_text("- check resets\n- check enables\n", encoding="utf-8", newline="\n")
    assert review_receipt_drift(detail, tmp_path) == ["guides"]
    assert current_review_receipt_identity(detail, tmp_path) != identity
    guide_file.unlink()  # a missing guide digests as empty: deletion is drift too
    assert review_receipt_drift(detail, tmp_path) == ["guides"]


def test_declaring_another_guide_for_the_focus_stales_the_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from booley.specialists.reviewer import REVIEW_GUIDES, ReviewGuide

    detail, _ = _guide_detail(tmp_path, monkeypatch)
    declared = REVIEW_GUIDES[("rtl", "bugs")]
    monkeypatch.setitem(
        REVIEW_GUIDES, ("rtl", "bugs"), (*declared, ReviewGuide("hunt", "rtl/h.md"))
    )
    assert review_receipt_drift(detail, tmp_path) == ["guides"]


def test_malformed_recorded_guides_are_stale(tmp_path: Path, monkeypatch) -> None:
    detail, _ = _guide_detail(tmp_path, monkeypatch)
    for bad in ("text", ["not a row"], [{"kind": "other", "path": "x.md", "digest": ""}]):
        detail["contract"]["guides"] = bad
        assert review_receipt_drift(detail, tmp_path) == ["guides"]
