"""Unit tests for the typed Python side of the B-Wave JSON contract."""

from __future__ import annotations

import json

import pytest

from booley.bwave.contract import decode_list_metadata
from booley.core.boundary import BoundaryError


def test_list_metadata_decoder_validates_and_matches_the_expected_scope() -> None:
    metadata = decode_list_metadata(
        json.dumps(
            {
                "data": {
                    "scope_prefix": "tb.uart",
                    "root_scopes": ["tb"],
                    "signal_count": 42,
                    "total_ticks": 900,
                }
            }
        )
    )

    assert metadata.display_scope == "tb.uart"
    assert metadata.contains_scope("tb") is True
    assert metadata.contains_scope("other") is False


def test_list_metadata_decoder_rejects_boolean_counts() -> None:
    with pytest.raises(BoundaryError, match="signal_count must be an integer"):
        decode_list_metadata(
            json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb",
                        "root_scopes": ["tb"],
                        "signal_count": True,
                        "total_ticks": 4,
                    }
                }
            )
        )
