"""Reviewer-authored, persisted lifecycle, and package disposition vocabularies."""

from collections.abc import Mapping
from types import MappingProxyType

# Where a finding of a diff-scoped (``--base``) review comes from: the change
# under review, or code that was already faulty at the merge-base.
ORIGIN_INTRODUCED = "introduced"
ORIGIN_PRE_EXISTING = "pre_existing"
ALL_ORIGINS: frozenset[str] = frozenset({ORIGIN_INTRODUCED, ORIGIN_PRE_EXISTING})
DISPOSITION_CURRENT = "current"
DISPOSITION_ADVISORY = "advisory"
DISPOSITION_DEFERRED = "deferred"
DISPOSITION_OUT_OF_SCOPE = "out_of_scope"
DISPOSITION_SUPERSEDED = "superseded"
ALL_DISPOSITIONS: frozenset[str] = frozenset(
    {DISPOSITION_CURRENT, DISPOSITION_ADVISORY, DISPOSITION_DEFERRED, DISPOSITION_OUT_OF_SCOPE}
)
PERSISTED_REVIEWER_DISPOSITIONS: frozenset[str] = ALL_DISPOSITIONS | {DISPOSITION_SUPERSEDED}
# Statuses a re-check writes for a finding it judged; ``fixed`` is final, the other two stay open.
STATUS_FIXED = "fixed"
STATUS_STILL_OPEN = "still_open"
STATUS_CANT_TELL = "cant_tell"
# The statuses a re-check sends to the model; every other status is carried over unchanged.
OPEN_STATUSES: frozenset[str] = frozenset(
    {DISPOSITION_CURRENT, STATUS_STILL_OPEN, STATUS_CANT_TELL}
)
REVIEW_DISPOSITIONS: frozenset[str] = frozenset(
    {"reported", "open", "fixed", "waived", "excluded"}
)
REVIEWER_TO_PACKAGE: Mapping[str, str] = MappingProxyType(
    {
        DISPOSITION_CURRENT: "open",
        DISPOSITION_ADVISORY: "reported",
        DISPOSITION_DEFERRED: "reported",
        DISPOSITION_OUT_OF_SCOPE: "reported",
        DISPOSITION_SUPERSEDED: "reported",
    }
)
