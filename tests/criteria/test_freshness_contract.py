"""Read-only verification freshness and placeholder eligibility."""

from pathlib import Path

from booley.criteria.freshness import (
    evaluate_verification_freshness,
    verification_freshness_eligible,
)
from booley.criteria.state import CriterionEntry
from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewInvocation,
    build_review_contract_detail,
)
from booley.flows.source_fingerprint import (
    compute_source_fingerprint,
)


def test_read_only_freshness_evaluator_reports_tb_drift_without_mutation(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    (work_dir / ".booley_project").mkdir(parents=True)
    (work_dir / "rtl").mkdir()
    (work_dir / "tb").mkdir()
    (work_dir / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    (work_dir / "tb" / "tb.sv").write_text("module tb; endmodule\n")
    (work_dir / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::design:0\n"
        "filesets:\n"
        "  rtl: {files: [rtl/dut.sv]}\n"
        "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
        "targets:\n"
        "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
        encoding="utf-8",
    )
    entry = CriterionEntry(
        met=True,
        mandatory=True,
        detail={
            SOURCE_FINGERPRINT_DETAIL_KEY: {
                "categories": ["rtl", "tb"],
                "target": "sim",
                "fingerprint": compute_source_fingerprint(work_dir, target="sim"),
            }
        },
    )
    before = repr(entry)
    current = evaluate_verification_freshness(
        "sim_pass_sim",
        entry,
        work_dir=work_dir,
        fingerprint_provider=compute_source_fingerprint,
    )
    (work_dir / "tb" / "tb.sv").write_text("module tb; // changed\nendmodule\n")

    result = evaluate_verification_freshness(
        "sim_pass_sim",
        entry,
        work_dir=work_dir,
        fingerprint_provider=compute_source_fingerprint,
    )

    assert current.stale is False
    assert current.changed_categories == ()
    assert result.stale is True
    assert result.changed_categories == ("tb",)
    assert "sources changed" in result.reason
    assert repr(entry) == before


def test_freshness_eligibility_distinguishes_gate_from_review_placeholders() -> None:
    pending_review = CriterionEntry(met=False, mandatory=True)
    assert verification_freshness_eligible(
        "review_rtl_bugs_done",
        pending_review,
        include_unobserved_review=True,
    )
    assert not verification_freshness_eligible(
        "review_rtl_bugs_done",
        pending_review,
        include_unobserved_review=False,
    )
    assert not verification_freshness_eligible(
        "sim_pass_sim",
        CriterionEntry(met=True, mandatory=False),
        include_unobserved_review=True,
    )


def test_read_only_freshness_evaluator_normalizes_malformed_project_config(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text("[broken", encoding="utf-8")
    entry = CriterionEntry(
        met=True,
        mandatory=True,
        detail={SOURCE_FINGERPRINT_DETAIL_KEY: {"categories": ["rtl"]}},
    )

    result = evaluate_verification_freshness(
        "sim_pass_sim",
        entry,
        work_dir=tmp_path,
        fingerprint_provider=compute_source_fingerprint,
    )

    assert result.stale
    assert result.changed_categories == ("rtl", "tb")
    assert result.current_evidence_identity
    assert "can no longer be resolved" in result.reason


def test_read_only_freshness_evaluator_prefers_version_4_reviewer_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    (tmp_path / ".booley_project").mkdir()
    source = tmp_path / "rtl" / "dut.sv"
    source.parent.mkdir()
    source.write_text("module dut; endmodule\n", encoding="utf-8")
    detail = {
        "review_detail_version": REVIEW_DETAIL_VERSION,
        "contract": build_review_contract_detail(
            ReviewInvocation(
                work_dir=tmp_path,
                category="rtl",
                focus="bugs",
                scope=("rtl/dut.sv",),
                mode="done",
            )
        ),
    }
    entry = CriterionEntry(True, True, detail=detail)

    assert not evaluate_verification_freshness(
        "review_rtl_bugs_done",
        entry,
        work_dir=tmp_path,
        fingerprint_provider=compute_source_fingerprint,
    ).stale
    source.write_text("module dut; // changed\nendmodule\n", encoding="utf-8")
    result = evaluate_verification_freshness(
        "review_rtl_bugs_done",
        entry,
        work_dir=tmp_path,
        fingerprint_provider=compute_source_fingerprint,
    )

    assert result.stale
    assert result.changed_categories == ("rtl",)
    assert result.review_dimensions == ("scope",)


from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
