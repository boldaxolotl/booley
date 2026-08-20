"""Strict parsing contracts for immutable review package version 2."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.harness.review_artifact import ReviewArtifactError, ReviewPackage


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
                "required": "optional",
                "outcome": "met",
                "freshness": "stale",
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
