"""Strict parsing contracts for immutable review package version 2."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.review.artifact import ReviewArtifactError, ReviewPackage


def _endpoint(path: str, revision: str, side: str) -> dict:
    return {
        "repository_path": path,
        "display_path": f"RTL/{path}",
        "revision": revision,
        "diff_path": f"/tmp/diffs/{side}/{path}",
        "workspace_path": f"/tmp/worktree/{path}" if side == "head" else None,
    }


def _change(action: str) -> dict:
    status = {
        "added": "A",
        "modified": "M",
        "deleted": "D",
        "renamed": "R087",
        "copied": "C087",
        "type-changed": "T",
    }[action]
    return {
        "repository": "rtl",
        "action": action,
        "content_kind": "regular",
        "presentation": "text",
        "similarity": 87 if action in {"renamed", "copied"} else None,
        "status": status,
        "old_endpoint": _endpoint("rtl/old.sv", "a" * 40, "base"),
        "new_endpoint": _endpoint("rtl/new.sv", "b" * 40, "head"),
    }


def _package() -> dict:
    return {
        "version": 2,
        "kind": "review",
        "slug": "unicode-λ",
        "feature_branch": "feature/[review]",
        "repositories": [
            {
                "name": "rtl",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "worktree": "/tmp/work tree",
            }
        ],
        "criteria": [
            {
                "category": "Review",
                "criterion": "review_security_done",
                "label": "RTL security review",
                "detail": "required review completed",
                "report_path": "/tmp/reports/security.md",
                "required": "optional",
                "outcome": "met",
                "freshness": "stale",
                "changed_categories": ["tb", "rtl", "tb"],
                "metric": "done independently of freshness",
            }
        ],
        "recipe_comparisons": [],
        "scope": {"decidable": True, "deviations": []},
        "commits": [],
        "changed_files": [_change("modified")],
        "developer_report_path": "/tmp/REPORT.md",
        "run_economics": "tokens=10",
        "health": {"dirty_worktree": []},
        "assessment": {
            "recommendation": "approve",
            "reason": "safe & grounded",
            "decision_blockers": [],
            "scope_deviations": [],
            "developer_summary": "handles `code`, [links], and λ",
            "uncertainties": "none",
            "optional_omissions": "none",
            "findings": [],
        },
        "html_path": None,
    }


@pytest.mark.parametrize(
    "action", ["added", "modified", "deleted", "renamed", "copied", "type-changed"]
)
def test_all_git_actions_parse_independently_from_content(action: str) -> None:
    value = _package()
    value["changed_files"] = [_change(action)]
    value["changed_files"][0]["content_kind"] = "submodule"
    value["changed_files"][0]["presentation"] = "binary"

    package = ReviewPackage.parse(value)

    assert package.changed_files[0].action == action
    assert package.changed_files[0].content_kind == "submodule"
    assert package.changed_files[0].presentation == "binary"


def test_outcome_and_freshness_remain_independent() -> None:
    package = ReviewPackage.parse(_package())

    assert package.criteria[0].outcome == "met"
    assert package.criteria[0].freshness == "stale"
    assert package.criteria[0].changed_categories == ("rtl", "tb")
    assert package.criteria[0].label == "RTL security review"
    assert package.criteria[0].detail == "required review completed"
    assert package.criteria[0].report_path == "/tmp/reports/security.md"
    assert package.to_dict()["criteria"][0]["status"] == "STALE (rtl, tb)"


def test_legacy_criterion_without_changed_categories_still_parses() -> None:
    value = _package()
    del value["criteria"][0]["changed_categories"]

    row = ReviewPackage.parse(value).criteria[0]

    assert row.changed_categories == ()
    assert row.to_dict()["status"] == "STALE"


def test_legacy_criterion_defaults_readable_fields() -> None:
    value = _package()
    for field in ("label", "detail", "report_path"):
        del value["criteria"][0][field]

    row = ReviewPackage.parse(value).criteria[0]

    assert row.label == row.criterion
    assert row.detail == ""
    assert row.report_path is None


@pytest.mark.parametrize("report_path", ["relative/report.md", "../report.md", ""])
def test_criterion_report_path_must_be_absolute(report_path: str) -> None:
    value = _package()
    value["criteria"][0]["report_path"] = report_path

    with pytest.raises(ReviewArtifactError, match="report_path"):
        ReviewPackage.parse(value)


@pytest.mark.parametrize("value", ["tb", ["tb", 1], [""]])
def test_malformed_changed_categories_are_rejected(value: object) -> None:
    package = _package()
    package["criteria"][0]["changed_categories"] = value

    with pytest.raises(ReviewArtifactError, match="changed_categories"):
        ReviewPackage.parse(package)


@pytest.mark.parametrize(
    ("field", "value"),
    [("action", "binary"), ("content_kind", "directory"), ("presentation", "submodule")],
)
def test_unknown_file_axis_values_are_rejected(field: str, value: str) -> None:
    package = _package()
    package["changed_files"][0][field] = value

    with pytest.raises(ReviewArtifactError, match=field):
        ReviewPackage.parse(package)


@pytest.mark.parametrize("path", ["/absolute/file.sv", "../escape.sv", ".", ""])
def test_malformed_repository_endpoints_are_rejected(path: str) -> None:
    package = _package()
    package["changed_files"][0]["new_endpoint"]["repository_path"] = path

    with pytest.raises(ReviewArtifactError):
        ReviewPackage.parse(package)


def test_similarity_is_bounded_and_required_for_rename() -> None:
    package = _package()
    package["changed_files"] = [_change("renamed")]
    package["changed_files"][0]["similarity"] = 101
    with pytest.raises(ReviewArtifactError, match="similarity"):
        ReviewPackage.parse(package)

    package["changed_files"][0]["similarity"] = None
    with pytest.raises(ReviewArtifactError, match="requires similarity"):
        ReviewPackage.parse(package)


def test_json_shape_round_trip_and_records_are_immutable() -> None:
    first = ReviewPackage.parse(_package())
    second = ReviewPackage.parse(first.to_dict())

    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.slug = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.health["dirty_worktree"] = ("changed",)  # type: ignore[index]


def test_cycle_comparison_round_trips_as_typed_package_data() -> None:
    value = _package()
    value["cycle_comparisons"] = [
        {
            "criterion": "cycle_count_binding",
            "target": "sim_coremark",
            "test": "coremark",
            "baseline_cycles": 100,
            "cycles": 90,
            "delta_cycles": -10,
            "delta_pct": -10.0,
            "checks": [],
        }
    ]

    package = ReviewPackage.parse(value)

    assert package.to_dict()["cycle_comparisons"][0]["delta_cycles"] == -10


def test_audit_round_trips_hostile_raw_json_and_legacy_absence() -> None:
    value = _package()
    assert ReviewPackage.parse(value).review_audit == ()
    raw = {"nested": [None, True, 17, "<script>[click](https://invalid)</script>"]}
    row = {
        "criterion": "review_rtl_bugs_clean",
        "collection": "rejected",
        "attempt_id": "attempt-1",
        "phase": "discovery",
        "ordinal": 2,
        "channel": "canonical",
        "raw": raw,
        "errors": ["invalid line"],
        "evidence": "/tmp/evidence.json",
    }
    value["review_audit"] = [row]
    package = ReviewPackage.parse(value)
    assert package.to_dict()["review_audit"] == [row]
    assert ReviewPackage.parse(package.to_dict()).to_dict() == package.to_dict()


@pytest.mark.parametrize(
    "updates", [{"ordinal": True}, {"raw": None, "errors": "bad"}, {"collection": "pending"}]
)
def test_audit_envelope_rejects_invalid_metadata(updates: dict) -> None:
    value = _package()
    value["review_audit"] = [
        {
            "criterion": "review_rtl_bugs_clean",
            "collection": "rejected",
            "attempt_id": "one",
            "phase": "discovery",
            "ordinal": 1,
            "channel": "canonical",
            "raw": None,
            "errors": ["bad"],
            **updates,
        }
    ]
    with pytest.raises(ReviewArtifactError):
        ReviewPackage.parse(value)


def test_review_briefing_keeps_audit_text_inert() -> None:
    from booley.review.triage_package import render_review_briefing

    value = _package()
    value["review_audit"] = [
        {
            "criterion": "review_rtl_bugs_clean",
            "collection": "filtered",
            "finding_id": "one",
            "reason": "source_scope",
            "explanation": "outside",
            "file": "[click](https://invalid)/<img>.sv",
            "summary": "<script>unsafe</script>",
            "phase": "discovery",
            "attempt_id": "one",
            "ordinal": 1,
            "evidence": "/tmp/evidence [one].json",
        }
    ]
    rendered = render_review_briefing(ReviewPackage.parse(value), [])
    assert "Filtered proposals and parsing rejections — do not affect Criteria" in rendered
    assert "\\[click\\]" in rendered
    assert "\\<script>unsafe\\<" in rendered
    assert "[Immutable reviewer evidence](/tmp/evidence%20%5Bone%5D.json)" in rendered
