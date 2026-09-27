"""Boundary tests for approved coverage-waiver configuration and document shapes."""

from __future__ import annotations

import unicodedata

import pytest

from booley.config.coverage_waiver_inputs import (
    approval_record_has_required_strings,
    formal_proof_references,
    is_safe_relative_posix,
    is_sha256,
    parse_coverage_waiver_config,
)

_DIGEST = "sha256:" + "a" * 64


def _approval_document(*, reason: str = "unreachable") -> dict[str, object]:
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
    return {
        "schema": "booley.coverage-waivers/v1",
        "source": "rtl/counter.sv",
        "source_sha256": _DIGEST,
        "approval": [record],
    }


def test_parse_optional_and_valid_coverage_waiver_config() -> None:
    assert parse_coverage_waiver_config({}) is None

    parsed = parse_coverage_waiver_config(
        {
            "coverage": {
                "waivers": {
                    "anchor": "project_data_repository",
                    "directory": "coverage-waivers",
                }
            }
        }
    )

    assert parsed is not None
    assert parsed.anchor == "project_data_repository"
    assert parsed.directory == "coverage-waivers"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"coverage": []}, "coverage must be a mapping"),
        ({"coverage": {"waivers": []}}, "requires anchor and directory"),
        (
            {"coverage": {"waivers": {"anchor": "rtl_repository"}}},
            "requires anchor and directory",
        ),
        (
            {
                "coverage": {
                    "waivers": {
                        "anchor": "rtl_repository",
                        "directory": "waivers",
                        "extra": True,
                    }
                }
            },
            "requires anchor and directory",
        ),
        (
            {"coverage": {"waivers": {"anchor": "elsewhere", "directory": "waivers"}}},
            "anchor is invalid",
        ),
        (
            {
                "coverage": {
                    "waivers": {
                        "anchor": "rtl_repository",
                        "directory": "../waivers",
                    }
                }
            },
            "safe relative POSIX path",
        ),
    ],
)
def test_parse_rejects_invalid_coverage_waiver_config(
    config: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_coverage_waiver_config(config)


@pytest.mark.parametrize(
    "value",
    ["", "/waivers", "C:/waivers", "a\\b", "a/../b", "a/./b", "a//b", "a\0b"],
)
def test_safe_relative_posix_rejects_unsafe_identifiers(value: str) -> None:
    assert not is_safe_relative_posix(value)


def test_safe_relative_posix_requires_nfc_and_accepts_normalized_path() -> None:
    decomposed = unicodedata.normalize("NFD", "café/waivers")

    assert not is_safe_relative_posix(decomposed)
    assert is_safe_relative_posix("café/waivers")


def test_formal_proof_references_use_the_shared_closed_shape() -> None:
    document = _approval_document()
    approvals = document["approval"]
    assert isinstance(approvals, list)
    record = approvals[0]
    assert isinstance(record, dict)

    assert formal_proof_references(document) == ("proofs/counter.sby",)
    assert approval_record_has_required_strings(record)
    assert is_sha256(_DIGEST)
    assert not is_sha256("sha256:not-a-digest")

    excluded = _approval_document(reason="excluded")
    assert formal_proof_references(excluded) == ()

    invalid = _approval_document()
    invalid_approvals = invalid["approval"]
    assert isinstance(invalid_approvals, list)
    invalid_record = invalid_approvals[0]
    assert isinstance(invalid_record, dict)
    invalid_record["justification"] = ""
    assert formal_proof_references(invalid) == ()
