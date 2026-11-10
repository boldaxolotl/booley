"""Shared fixture: register a test-only multi-word review verdict."""

from __future__ import annotations

import pytest


def register_no_major(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register `no_major` between `clean` and `done`: corrective, blocks MAJOR and above."""
    import booley.evidence.review_verdicts as model

    monkeypatch.setattr(
        model,
        "VERDICT_RULES",
        {
            "clean": model.VERDICT_RULES["clean"],
            "no_major": model.VerdictRule(corrective=True, blocking_severity=model.SEVERITY_MAJOR),
            "done": model.VERDICT_RULES["done"],
        },
    )
