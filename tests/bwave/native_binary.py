"""Shared prerequisite for native B-Wave contract and session tests."""

from pathlib import Path

import pytest

from booley.dev_support.test_profiles import BWAVE_BUILD, native_test_binary


def require_native_binary() -> Path:
    """Select the current checkout's debug build or explain its absence."""
    binary = native_test_binary(Path(__file__).resolve().parents[2])
    if not binary.is_file():
        pytest.skip(f"native B-Wave debug binary not built; run {' '.join(BWAVE_BUILD)}")
    return binary
