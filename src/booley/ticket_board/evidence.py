"""Evidence collection for adversarial reviewer.

Builds a tamper-resistant evidence bundle from ticket metadata and
step logs. Extracted from operations.py for single-responsibility (P8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter


def _extract_acceptance_criteria(tio, slug):
    """Extract acceptance criteria from ticket markdown body."""
    criteria = []
    candidates = [tio.logs_dir / slug / "ticket.md"]
    if tio.tickets_dir:
        candidates.extend(tio.tickets_dir.rglob(f"*{slug}*"))
    for candidate_path in candidates:
        candidate = Path(candidate_path)
        if not candidate.is_file():
            continue
        with candidate.open(encoding="utf-8") as f:
            _, body = parse_frontmatter(f.read())
        in_criteria = False
        for line in body.splitlines():
            if line.strip().lower().startswith("## acceptance criteria"):
                in_criteria = True
                continue
            if in_criteria and line.strip().startswith("## "):
                break
            if in_criteria and line.strip().startswith("- "):
                criteria.append(line.strip()[2:].strip())
        break
    return criteria


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
            "acceptance_criteria": _extract_acceptance_criteria(tio, slug),
        },
    }
