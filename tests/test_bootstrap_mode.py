"""Tests for booley.runtime.bootstrap_mode — TOP_LEVEL vs NESTED detection."""

from __future__ import annotations

import pytest

from booley.runtime.bootstrap_mode import BootstrapMode, should_run_outer_bookkeeping

# The marker is a strict "1" check: anything else is TOP_LEVEL, which guards against
# accidental truthy reinterpretation.
_MODES = [
    pytest.param(None, BootstrapMode.TOP_LEVEL, id="unset"),
    pytest.param("1", BootstrapMode.NESTED, id="one"),
    *(
        pytest.param(value, BootstrapMode.TOP_LEVEL, id=f"other-{value or 'empty'}")
        for value in ("", "0", "true", "yes", "2")
    ),
]


@pytest.mark.parametrize(("marker", "expected"), _MODES)
def test_detect_reads_the_nested_marker(monkeypatch, marker, expected):
    if marker is None:
        monkeypatch.delenv("BOOLEY_MCP_NESTED", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_MCP_NESTED", marker)

    assert BootstrapMode.detect() is expected
    # Chokepoint behavior: outer bookkeeping runs iff TOP_LEVEL.
    assert should_run_outer_bookkeeping() is (expected is BootstrapMode.TOP_LEVEL)
