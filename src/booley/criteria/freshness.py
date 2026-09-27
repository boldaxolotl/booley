"""Read-only verification freshness policy shared by gates and presentation."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str_list
from booley.criteria.categories import verification_fingerprint_categories
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.evidence.review_receipt import (
    ReviewTicketError,
    current_review_receipt_identity,
    review_receipt_drift,
)
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime.shared_infra import _load_rtl_config
from booley.targets.flow_names import config_section

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationFreshness:
    """Read-only comparison of recorded verification with current inputs."""

    stale: bool
    changed_categories: tuple[str, ...] = ()
    reason: str = ""
    current_source_fingerprint: dict[str, Any] = field(default_factory=dict)
    current_evidence_identity: str = ""
    review_dimensions: tuple[str, ...] = ()


def _entry_detail(entry: Any) -> Mapping[str, Any]:
    detail = entry.get("detail") if isinstance(entry, Mapping) else entry.detail
    return detail if isinstance(detail, Mapping) else {}


def _identity(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def review_policy_digest(work_dir: Path, category: str) -> str:
    """Return the current policy identity used by one Reviewer invocation."""
    if category != "tb":
        return _identity({})
    try:
        cfg = _load_rtl_config(work_dir)
    except ImportError:
        cfg = None
    flows = as_dict((cfg or {}).get("flows"), default={}) or {}
    sim = config_section(flows, "sim")
    return _identity(
        {
            "pass_sentinels": as_str_list(sim.get("pass_sentinels")),
            "fail_sentinels": as_str_list(sim.get("fail_sentinels")),
            "trace_files": as_str_list(sim.get("trace_files")),
        }
    )


def _review_freshness(
    detail: Mapping[str, Any],
    *,
    work_dir: Path,
    categories: set[str],
) -> VerificationFreshness | None:
    try:
        contract = detail.get("contract")
        category = contract.get("category", "") if isinstance(contract, Mapping) else ""
        policy_digest = review_policy_digest(work_dir, category)
        changed = review_receipt_drift(detail, work_dir, tb_policy_digest=policy_digest)
        identity_value = current_review_receipt_identity(
            detail, work_dir, tb_policy_digest=policy_digest
        )
    except ReviewTicketError as exc:
        return VerificationFreshness(
            True, tuple(sorted(categories)), str(exc), review_dimensions=("ticket",)
        )
    except (FuseSocError, OSError) as exc:
        return VerificationFreshness(
            True,
            tuple(sorted(categories)),
            f"Reviewer source context can no longer be resolved: {exc}",
            review_dimensions=("source_context",),
        )
    identity = _identity(identity_value)
    if changed:
        return VerificationFreshness(
            True,
            tuple(sorted(categories)),
            "Reviewer requirement changed after the recorded verdict "
            f"({', '.join(changed)}); re-run Reviewer.",
            current_evidence_identity=identity,
            review_dimensions=tuple(changed),
        )
    if detail.get("review_detail_version") == 4:
        return VerificationFreshness(False, current_evidence_identity=identity)
    return None


def _source_freshness(
    detail: Mapping[str, Any],
    *,
    work_dir: Path,
    fingerprints: dict[str | None, dict[str, Any]],
    categories: set[str],
    fingerprint_provider: Callable[..., dict[str, Any]],
) -> VerificationFreshness | None:
    stamp = detail.get(SOURCE_FINGERPRINT_DETAIL_KEY)
    target = stamp.get("target") if isinstance(stamp, Mapping) else None
    target = target if isinstance(target, str) and target else None
    try:
        if target not in fingerprints:
            fingerprints[target] = fingerprint_provider(work_dir, target=target)
        current = fingerprints[target]
    except FuseSocError as exc:
        return VerificationFreshness(
            True,
            tuple(sorted(categories)),
            f"Target-specific source fingerprint can no longer be resolved: {exc}. "
            "Re-run the relevant Flow or Specialist with a valid Target.",
        )
    except OSError:
        logger.debug("Could not compute final source fingerprint", exc_info=True)
        return VerificationFreshness(False)
    return _compare_source_stamp(stamp, current=current, categories=categories)


def _compare_source_stamp(
    stamp: object,
    *,
    current: dict[str, Any],
    categories: set[str],
) -> VerificationFreshness:
    identity = _identity(current)
    if not isinstance(stamp, Mapping):
        return VerificationFreshness(
            True,
            tuple(sorted(categories)),
            "Passing verification criterion has no source fingerprint; "
            "re-run the relevant Flow or Specialist.",
            current,
            identity,
        )
    previous = stamp.get("fingerprint", {})
    stamped = stamp.get("categories", [])
    if not isinstance(previous, Mapping) or not isinstance(stamped, list):
        return VerificationFreshness(
            True,
            tuple(sorted(categories)),
            "Passing verification criterion has an invalid source fingerprint; "
            "re-run the relevant Flow or Specialist.",
            current,
            identity,
        )
    changed = tuple(
        sorted(
            item
            for item in stamped
            if (previous.get(item, {}) or {}).get("digest")
            != (current.get(item, {}) or {}).get("digest")
        )
    )
    reason = (
        "RTL/testbench sources changed after the last passing verification "
        "evidence; re-run the relevant Flow or Specialist."
        if changed
        else ""
    )
    return VerificationFreshness(bool(changed), changed, reason, current, identity)


def evaluate_verification_freshness(
    key: str,
    entry: Any,
    *,
    work_dir: Path,
    fingerprint_provider: Callable[..., dict[str, Any]],
    fingerprints: dict[str | None, dict[str, Any]] | None = None,
) -> VerificationFreshness | None:
    """Compare one Criterion's immutable evidence with its current inputs."""
    categories = verification_fingerprint_categories(key)
    if not categories:
        return None
    detail = _entry_detail(entry)
    if key.startswith(("review_rtl_", "review_tb_")):
        receipt = _review_freshness(detail, work_dir=work_dir, categories=categories)
        if receipt is not None:
            return receipt
    return _source_freshness(
        detail,
        work_dir=work_dir,
        fingerprints=fingerprints if fingerprints is not None else {},
        categories=categories,
        fingerprint_provider=fingerprint_provider,
    )
