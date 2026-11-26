"""Boundary tests for approved coverage-waiver configuration and document shapes."""

from __future__ import annotations

import unicodedata

import pytest

from booley.config.coverage_waiver_inputs import (
    approval_record_has_required_strings,
    is_safe_relative_posix,
    is_sha256,
    parse_coverage_waiver_config,
    proof_references,
)

_DIGEST = "sha256:" + "a" * 64


def _approval_record(reason: str = "unreachable", **fields: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": "counter-unreachable",
        "target": "acme:lib:counter:1#sim",
        "point_id": "cp1:point",
        "reason": reason,
        "justification": "Reviewed.",
        "approved_by": "human@example.invalid",
        "approved_at": "2026-09-25T00:00:00Z",
        "approval_ref": "review-738",
    }
    if reason == "unreachable":
        record["proof"] = {
            "kind": "formal",
            "reference": "proofs/counter.sby#cover_17",
            "sha256": _DIGEST,
        }
    return {**record, **fields}


def _approval_document(record: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "booley.coverage-waivers/v1",
        "source": "rtl/counter.sv",
        "source_sha256": _DIGEST,
        "approval": [record],
    }


def _waivers(**keys: object) -> dict[str, object]:
    return {"coverage": {"waivers": keys}}


def test_parse_optional_and_valid_coverage_waiver_config() -> None:
    assert parse_coverage_waiver_config({}) is None

    parsed = parse_coverage_waiver_config(
        _waivers(anchor="project_data_repository", directory="coverage-waivers")
    )

    assert parsed is not None
    assert parsed.anchor == "project_data_repository"
    assert parsed.directory == "coverage-waivers"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"coverage": []}, "coverage must be a mapping"),
        ({"coverage": {"waivers": []}}, "requires anchor and directory"),
        (_waivers(anchor="rtl_repository"), "requires anchor and directory"),
        (
            _waivers(anchor="rtl_repository", directory="waivers", extra=True),
            "requires anchor and directory",
        ),
        (_waivers(anchor="elsewhere", directory="waivers"), "anchor is invalid"),
        (_waivers(anchor="rtl_repository", directory="../waivers"), "safe relative POSIX path"),
        (_waivers(anchor="rtl_repository", directory="."), "safe relative POSIX path"),
    ],
)
def test_parse_rejects_invalid_coverage_waiver_config(
    config: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_coverage_waiver_config(config)


@pytest.mark.parametrize(
    "value",
    [
        *("", ".", "/waivers", "C:/waivers", "a\\b", "a/../b", "a/./b", "a//b", "a\0b"),
        unicodedata.normalize("NFD", "café/waivers"),
    ],
)
def test_safe_relative_posix_rejects_unsafe_identifiers(value: str) -> None:
    assert not is_safe_relative_posix(value)
    assert is_safe_relative_posix("café/waivers")


def test_proof_references_use_the_shared_closed_shape() -> None:
    record = _approval_record()

    assert proof_references(_approval_document(record)) == ("proofs/counter.sby",)
    assert approval_record_has_required_strings(record)
    assert is_sha256(_DIGEST)
    assert not is_sha256("sha256:not-a-digest")

    assert proof_references(_approval_document(_approval_record("excluded"))) == ()
    assert proof_references(_approval_document(_approval_record(justification=""))) == ()


def _proof_of_kind(kind: str) -> dict[str, object]:
    proof = {"kind": kind, "reference": "proofs/counter.md", "sha256": _DIGEST}
    return _approval_document(_approval_record(proof=proof))


def test_proof_references_accept_review_proof_and_reject_unknown_kinds() -> None:
    """ADR 0066: `review` proofs are first-class; any other kind stays invalid."""
    assert proof_references(_proof_of_kind("review")) == ("proofs/counter.md",)
    assert proof_references(_proof_of_kind("hunch")) == ()
