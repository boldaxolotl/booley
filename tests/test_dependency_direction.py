"""Static boundary check for the session-issuance facade."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src/booley"


def test_session_issuance_is_one_deep_boundary_for_independent_callers() -> None:
    """Deleting the facade breaks four callers with distinct policy needs."""
    consumers = {
        "harness/setup/interactive.py": {"preview", "issue_prepared"},
        "runtime/inspection.py": {"validate", "requested_license", "keeper_image"},
        "runtime/session_runtime.py": {"authenticate", "validate", "labels"},
        "runtime/session_refresh.py": {
            "load_issued_snapshot",
            "load_recovery_snapshot",
            "issuance_from_document",
        },
    }
    for relative, capabilities in consumers.items():
        source = (_PACKAGE / relative).read_text(encoding="utf-8")
        assert "session_issuance" in source
        assert all(f".{capability}" in source for capability in capabilities)

    eda_provider = (_PACKAGE / "eda/provisioning/session_requirements.py").read_text(
        encoding="utf-8"
    )
    runtime_authority = {"def issue(", "def authenticate(", "def invalidate_project("}
    assert all(symbol not in eda_provider for symbol in runtime_authority)
