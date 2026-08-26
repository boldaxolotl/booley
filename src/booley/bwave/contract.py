"""The bwave process-boundary contract, Python side.

Exit codes and load-bearing stderr markers shared between the Rust binary
(``crates/bwave``) and every Python consumer. The Rust half of the contract
lives in ``crates/bwave/src/cache.rs`` (``no_match_message``,
``no_signals_in_store_message``) and the exit sites in
``crates/bwave/src/main.rs``; a Rust unit test in ``cache.rs``
(``contract`` tests at the bottom of its test mod) pins the marker
substrings, and ``tests/bwave/test_contract.py`` pins them
cross-process against the built binary.

Change anything here ONLY together with the Rust side — a reworded
diagnostic that loses a marker silently turns e.g. coverage_analyst's
discovery fallback into a hard error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import NoReturn

from booley.core.boundary import (
    BoundaryError,
    as_str,
    as_str_list,
    is_str_list,
    require_dict,
    require_int,
)

# Exit codes of the bwave binary (and of wrapper errors that mirror it):
#   0 — success, including a *partial* pattern miss (some -s matched).
#   1 — environment / I-O: cannot open input, cannot load store, write failed.
#   2 — caller input: bad flag/glob/time/radix, a -s/--clock/trigger pattern
#       that matches nothing in this trace, a query against a zero-signal
#       store, or building a store that would be unqueryable.
EXIT_OK = 0
EXIT_ENV = 1
EXIT_USAGE = 2

# Substring of the Rust total-miss diagnostic ("ERROR: no signals match
# pattern(s) ..."). coverage_analyst keys its discovery fallback on
# rc == EXIT_USAGE plus this marker. Compare against lowercased stderr.
NO_MATCH_MARKER = "no signals match"

# Substring of the zero-signal-store diagnostic ("ERROR: waveform store has
# no signals ..."), emitted by queries (exit 2) and by `list` (exit 0, both
# text and JSON modes put it on stderr).
NO_SIGNALS_IN_STORE_MARKER = "has no signals"

# Prefix of the stderr line naming the common scope of the listed signals
# ("# scope: <top>"). bwave_sessions._trace_identity parses the design
# identity out of `list --tree` stderr through this prefix.
SCOPE_LINE_PREFIX = "# scope: "


@dataclass(frozen=True)
class BWaveListMetadata:
    """Validated metadata returned by ``bwave list --format json``."""

    scope_prefix: str
    root_scopes: tuple[str, ...]
    signal_count: int
    total_ticks: int

    @property
    def display_scope(self) -> str:
        """The most precise human-readable hierarchy identity available."""
        if self.scope_prefix:
            return self.scope_prefix
        if self.root_scopes:
            return ", ".join(self.root_scopes)
        return "<top-level signals>"

    def contains_scope(self, expected: str) -> bool:
        """Whether the listed hierarchy contains the expected DUT scope."""
        expected = expected.strip().rstrip(".")
        if not expected:
            return False
        return (
            self.scope_prefix == expected
            or self.scope_prefix.startswith(f"{expected}.")
            or expected in self.root_scopes
        )


def decode_list_metadata(text: str) -> BWaveListMetadata:
    """Decode and validate one B-Wave JSON ``list`` response."""
    payload = require_dict(json.loads(text), field="B-Wave list response")
    data = require_dict(payload.get("data"), field="B-Wave list response.data")
    scope_prefix = as_str(data.get("scope_prefix"))
    if scope_prefix is None:
        raise BoundaryError("B-Wave list response.data.scope_prefix must be a string")
    raw_roots = data.get("root_scopes")
    if not is_str_list(raw_roots):
        raise BoundaryError("B-Wave list response.data.root_scopes must be a string list")
    return BWaveListMetadata(
        scope_prefix=scope_prefix.strip(),
        root_scopes=tuple(scope.strip() for scope in as_str_list(raw_roots) if scope.strip()),
        signal_count=_require_non_negative_int(data, "signal_count"),
        total_ticks=_require_non_negative_int(data, "total_ticks"),
    )


def decode_trace_metadata(text: str) -> BWaveListMetadata:
    """Decode the compact ``TRACE_METADATA`` marker payload."""
    data = require_dict(json.loads(text), field="TRACE_METADATA")
    top_scope = as_str(data.get("top_scope"))
    if top_scope is None:
        raise BoundaryError("TRACE_METADATA.top_scope must be a string")
    return BWaveListMetadata(
        scope_prefix=top_scope.strip(),
        root_scopes=(),
        signal_count=_require_non_negative_int(data, "signal_count", field="TRACE_METADATA"),
        total_ticks=_require_non_negative_int(data, "total_ticks", field="TRACE_METADATA"),
    )


def _require_non_negative_int(
    data: dict,
    key: str,
    *,
    field: str = "B-Wave list response.data",
) -> int:
    value = require_int(data.get(key), field=f"{field}.{key}")
    if value < 0:
        raise BoundaryError(f"{field}.{key} must be non-negative")
    return value


def exit_usage(message: str) -> NoReturn:
    """Exit with the caller-input code the Rust binary uses (2, not 1).

    ``sys.exit("ERROR: ...")`` exits 1, which this contract reserves for
    environment/I-O failures — so wrapper-side input errors (a glob that
    matches nothing, registering a raw VCD) must not use it, or scripted
    callers cannot tell "my input was wrong" from "the B-Wave executable broke".
    """
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_USAGE)
