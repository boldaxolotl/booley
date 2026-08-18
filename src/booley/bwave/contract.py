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

import sys
from typing import NoReturn

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


def exit_usage(message: str) -> NoReturn:
    """Exit with the caller-input code the Rust binary uses (2, not 1).

    ``sys.exit("ERROR: ...")`` exits 1, which this contract reserves for
    environment/I-O failures — so wrapper-side input errors (a glob that
    matches nothing, registering a raw VCD) must not use it, or scripted
    callers cannot tell "my input was wrong" from "the B-Wave executable broke".
    """
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_USAGE)
