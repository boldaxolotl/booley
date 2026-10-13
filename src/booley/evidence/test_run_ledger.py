"""The Test Run ledger an observation of a Goal stores (ADR 0072).

A Goal that is judged by Test Runs carries, in the detail of each of its
observations, one entry per frozen Test Run with its outcome so far and the
earlier observations that contributed. Goal Mode writes and verifies it
(:mod:`booley.goals.test_run_evidence`); the review package renders it. This
module holds the stored format and its one reader, for both.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_list,
    require_positive_int,
    require_str_value,
)

#: Detail key of the materialized per-run outcomes of a Goal's frozen Test Runs.
LEDGER_DETAIL_KEY = "test_run_ledger"
LEDGER_SCHEMA = "booley.goal-test-run-ledger/v1"

RunOutcome = Literal["passed", "failed", "missing"]
_OUTCOMES: tuple[RunOutcome, ...] = ("passed", "failed", "missing")


class LedgerFormatError(ValueError):
    """A stored Test Run ledger does not have the ledger format."""


@dataclass(frozen=True)
class StoredLedger:
    """A Test Run ledger as an observation stores it, decoded and validated.

    ``runs`` are the per-run outcomes in the stored form (``name``,
    ``fingerprint``, ``outcome``); ``source_evidence`` the linked
    contributors as (ledger sequence, digest).
    """

    runs: tuple[dict[str, str], ...]
    source_evidence: tuple[tuple[int, str], ...]


def decode_ledger(detail: Mapping[str, Any]) -> StoredLedger | None:
    """The ledger *detail* stores, or ``None`` when it stores none.

    The one reader of a stored ledger. Raises :class:`LedgerFormatError`
    for a ledger with another schema or shape: stored evidence is external
    input, and a malformed ledger judges nothing.
    """
    if LEDGER_DETAIL_KEY not in detail:
        return None
    where = LEDGER_DETAIL_KEY
    try:
        ledger = require_dict(detail[LEDGER_DETAIL_KEY], field=where)
        if set(ledger) != {"schema", "runs", "source_evidence"}:
            raise LedgerFormatError(f"{where} must hold schema, runs and source_evidence")
        if ledger["schema"] != LEDGER_SCHEMA:
            raise LedgerFormatError(f"{where} has schema {ledger['schema']!r}")
        runs = tuple(
            _stored_run(run, f"{where}.runs")
            for run in require_list(ledger["runs"], field=f"{where}.runs")
        )
        links = tuple(
            _stored_link(link, f"{where}.source_evidence")
            for link in require_list(ledger["source_evidence"], field=f"{where}.source_evidence")
        )
    except BoundaryError as exc:
        raise LedgerFormatError(str(exc)) from None
    return StoredLedger(runs, links)


def _stored_run(raw: object, where: str) -> dict[str, str]:
    run = require_dict(raw, field=where)
    if set(run) != {"name", "fingerprint", "outcome"} or run["outcome"] not in _OUTCOMES:
        raise LedgerFormatError(f"{where} entries hold name, fingerprint and a known outcome")
    return {
        "name": require_str_value(run["name"], field=f"{where}.name"),
        "fingerprint": require_str_value(run["fingerprint"], field=f"{where}.fingerprint"),
        "outcome": str(run["outcome"]),
    }


def _stored_link(raw: object, where: str) -> tuple[int, str]:
    link = require_dict(raw, field=where)
    if set(link) != {"sequence", "sha256"}:
        raise LedgerFormatError(f"{where} entries hold sequence and sha256")
    return (
        require_positive_int(link["sequence"], field=f"{where}.sequence"),
        require_str_value(link["sha256"], field=f"{where}.sha256"),
    )
