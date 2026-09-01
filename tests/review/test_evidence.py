"""Tests for the immutable review-evidence contract."""

from __future__ import annotations

import pytest

from booley.review.evidence import ReviewEvidenceError, build_review_evidence


def _sources(tmp_path):
    sources = {}
    for name in ("ticket", "diff", "commits", "files", "status"):
        path = tmp_path / f"{name}.txt"
        path.write_text(f"{name}\n", encoding="utf-8")
        sources[name] = path
    return sources


def test_manifest_is_versioned_and_stably_ordered(tmp_path):
    package = build_review_evidence(
        slug="demo",
        base_sha="a" * 40,
        head_sha="b" * 40,
        source_sha256="c" * 64,
        sources=_sources(tmp_path),
    )

    manifest = package.manifest.as_dict()
    assert manifest["version"] == 1
    assert [item["name"] for item in manifest["items"]] == [
        "commits",
        "diff",
        "files",
        "status",
        "ticket",
    ]
    package.verify()


def test_missing_required_input_is_rejected(tmp_path):
    sources = _sources(tmp_path)
    del sources["status"]

    with pytest.raises(ReviewEvidenceError, match=r"missing required inputs.*status"):
        build_review_evidence(
            slug="demo",
            base_sha="a",
            head_sha="b",
            source_sha256="c",
            sources=sources,
        )


def test_changed_input_is_rejected_before_consumption(tmp_path):
    sources = _sources(tmp_path)
    package = build_review_evidence(
        slug="demo",
        base_sha="a",
        head_sha="b",
        source_sha256="c",
        sources=sources,
    )
    sources["diff"].write_text("changed\n", encoding="utf-8")

    with pytest.raises(ReviewEvidenceError, match="changed after collection: diff"):
        package.verify()


def test_directory_digest_covers_relative_paths_and_contents(tmp_path):
    sources = _sources(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "one.json").write_text("{}\n", encoding="utf-8")
    sources["flow_reports"] = reports
    package = build_review_evidence(
        slug="demo",
        base_sha="a",
        head_sha="b",
        source_sha256="c",
        sources=sources,
    )
    (reports / "one.json").write_text('{"changed": true}\n', encoding="utf-8")

    with pytest.raises(ReviewEvidenceError, match="flow_reports"):
        package.verify()
