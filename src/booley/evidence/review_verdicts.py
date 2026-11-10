"""The review verdict model: ordered verdicts, their gate rules, and review-key parsing.

A review criterion key is `review_<family>_<verdict>`. This module is the only place
that knows how a verdict is spelled inside a key, which verdicts exist, how strict each
one is, and which open findings block under it. Adding a verdict is a change to
`VERDICT_RULES` (and the `ReviewVerdict` literal, which a test keeps in step).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_MAJOR = "MAJOR"
SEVERITY_MINOR = "MINOR"
# Higher rank = more severe.
SEVERITY_RANK: Mapping[str, int] = {SEVERITY_MINOR: 0, SEVERITY_MAJOR: 1, SEVERITY_CRITICAL: 2}

REVIEW_KEY_PREFIX = "review_"


@dataclass(frozen=True)
class VerdictRule:
    """How one verdict gates a review.

    `corrective`: open findings must be resolved (the verify loop runs).
    `blocking_severity`: the lowest severity that blocks; None means nothing blocks.
    """

    corrective: bool
    blocking_severity: str | None


# Strictest first: position is the verdict's rank. Every function below reads this table
# at call time, so a test can register an extra verdict by replacing it.
VERDICT_RULES: Mapping[str, VerdictRule] = {
    "clean": VerdictRule(corrective=True, blocking_severity=SEVERITY_MINOR),
    "done": VerdictRule(corrective=False, blocking_severity=None),
}

# The typed spelling of the table's keys; a test keeps the two in step.
ReviewVerdict = Literal["clean", "done"]
REVIEW_VERDICTS: tuple[ReviewVerdict, ...] = cast(
    "tuple[ReviewVerdict, ...]", tuple(VERDICT_RULES)
)


def verdict_rank(verdict: str) -> int:
    """Return the verdict's position in strictness order (0 = strictest)."""
    for rank, name in enumerate(VERDICT_RULES):
        if name == verdict:
            return rank
    raise ValueError(f"unknown review verdict {verdict!r}; expected one of {list(VERDICT_RULES)}")


def split_review_key(key: str) -> tuple[str, str] | None:
    """Split `review_<family>_<verdict>` into (family key, verdict); None for any other key.

    A bare review key (`review_rtl_bugs`) and a non-review key (`lint_clean`) return None.
    """
    if not key.startswith(REVIEW_KEY_PREFIX):
        return None
    for verdict in VERDICT_RULES:
        family = key.removesuffix(f"_{verdict}")
        if family != key and len(family) > len(REVIEW_KEY_PREFIX):
            return family, verdict
    return None


def review_key(family: str, verdict: str) -> str:
    """Build the criterion key of a review family under a verdict."""
    verdict_rank(verdict)
    return f"{family}_{verdict}"


def review_keys(family: str) -> tuple[str, ...]:
    """Return the family's key under every verdict, strictest first."""
    return tuple(review_key(family, verdict) for verdict in VERDICT_RULES)


def review_family(key: str) -> str:
    """Return the key without its verdict suffix; a key with no verdict is returned as is."""
    split = split_review_key(key)
    return key if split is None else split[0]


def is_corrective_review_key(key: str) -> bool:
    """True when the key is a review key whose verdict requires findings to be resolved."""
    split = split_review_key(key)
    return split is not None and VERDICT_RULES[split[1]].corrective


def is_advisory_review_key(key: str) -> bool:
    """True when the key is a review key whose verdict does not require fixes."""
    split = split_review_key(key)
    return split is not None and not VERDICT_RULES[split[1]].corrective


def finding_blocks(severity: str | None, verdict: str) -> bool:
    """True when an open finding of this severity blocks the review under the verdict.

    An unknown or missing severity blocks under any gating verdict (fail closed).
    """
    threshold = VERDICT_RULES[verdict].blocking_severity
    if threshold is None:
        return False
    rank = SEVERITY_RANK.get((severity or "").upper())
    return rank is None or rank >= SEVERITY_RANK[threshold]


def blocking_findings(rows: Iterable[Mapping[str, Any]], verdict: str) -> list[Mapping[str, Any]]:
    """Return the open finding rows the verdict requires resolving.

    A non-corrective verdict sets no gate, so its human-directed verify keeps every row.
    """
    if not VERDICT_RULES[verdict].corrective:
        return list(rows)
    return [row for row in rows if finding_blocks(row.get("severity"), verdict)]
