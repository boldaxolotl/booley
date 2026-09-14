"""Evidence collection for adversarial reviewer.

Builds a tamper-resistant evidence bundle from ticket metadata and
step logs. Extracted from operations.py for single-responsibility (P8).
"""

from __future__ import annotations

from typing import Any


def op_collect_evidence(tio: Any, slug: str) -> dict[str, Any] | None:
    """Collect ticket evidence that still has an authoritative source."""
    entry = tio.find_ticket(slug)
    if not entry:
        return None

    return {
        "ticket": {
            "type": entry.get("type", "feature"),
            "scope": entry.get("scope", []),
            "spec": entry.get("spec", None),
            "test": entry.get("test", {}),
            "acceptance_criteria": [
                *entry.get("criteria", {}).get("mandatory", {}),
                *entry.get("criteria", {}).get("optional", {}),
            ],
        },
    }
