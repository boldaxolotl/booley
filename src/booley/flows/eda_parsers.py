"""EDA log/report parsers shared by the built-in Booley Flows (private).

The pure text-extraction blocks of the built-in Flows: sim sentinel verdicts
(re-exported from ``booley.flows.sim.result``), Verilator and Verible
warning/error parsing (single source of truth — ``booley.flows.lint.flow`` imports
these regexes), compiler-error gists (shared with ``booley.flows.sim.flow``),
and Yosys area extraction (re-exported from ``booley.flows.synth.backends.yosys.parsing``).

Formerly the public ``booley.adapterlib.parsers`` — the one module the
built-in flows imported from the adapter library; it moved here as a private
module when the project-native adapters were dropped (ADR 0039).
"""

from __future__ import annotations

import re

# Verilator warning lines (also the built-in lint parser's regex):
#   %Warning-UNUSEDSIGNAL: module_a.sv:42:5: Signal is not used: 'foo'
VERILATOR_WARNING_RE = re.compile(
    r"%Warning-(?P<rule>[A-Z0-9_]+):\s+"
    r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<message>.+)"
)

# Verilator emits ``%Error`` (and the summary ``%Error: Exiting due to N
# error(s)``) for hard failures — undeclared signals, parse errors — and exits
# non-zero. A warnings-only parser yields zero findings on such a run and
# would score it as a clean PASS (QA-7); scan for errors separately.
VERILATOR_ERROR_RE = re.compile(r"^%Error.*", re.MULTILINE)


def first_error_line(output: str) -> str | None:
    """Return the first Verilator ``%Error`` line in *output*, if any."""
    match = VERILATOR_ERROR_RE.search(output)
    return match.group(0).strip() if match else None


# Verible lint findings (also the built-in lint parser's regex for Targets
# whose flow_options.tool is verible, ADR 0033). Under ``--parse_fatal
# --lint_fatal=false`` findings arrive with rc 0, one per line:
#   rtl/top.sv:4:11: Interface names must end with _if. [interface-name-style]
# The column may be a range (``line:col-col``); the leading column is kept.
VERIBLE_FINDING_RE = re.compile(
    r"^(?P<file>[^\s:][^:\n]*):(?P<line>\d+):(?P<col>\d+)(?:-\d+)?:\s+"
    r"(?P<message>.+?)\s+\[(?P<rule>[^\]\s]+)\]\s*$",
    re.MULTILINE,
)

# Verible parse failures (``--parse_fatal`` → non-zero rc) carry a location
# but no ``[rule]`` suffix:
#   rtl/top.sv:3:1: syntax error at token "endmodule"
# A findings-only parser yields zero findings on such a run and would score
# it as a clean PASS (QA-7); scan for the error line separately.
VERIBLE_ERROR_RE = re.compile(
    r"^[^\s:][^:\n]*:\d+:\d+(?:-\d+)?:\s+"
    r"(?:syntax error|preprocessing error|token recognition error).*$",
    re.MULTILINE | re.IGNORECASE,
)


def verible_first_error_line(output: str) -> str | None:
    """Return the first Verible parse-error line in *output*, if any."""
    match = VERIBLE_ERROR_RE.search(output)
    return match.group(0).strip() if match else None


def extract_error_gist(error_output: str) -> str:
    """Extract a one-line error gist from compiler output.

    Looks for common error patterns (Verilator, Icarus, sv2v) and returns the
    first meaningful error line, truncated for display — a ready-made
    ``first_error`` for Simulation responses, including Elaboration Check mode.
    """
    if not error_output:
        return ""
    error_re = re.compile(
        r"(?:%Error[^:]*:\s*\S+:\d+:\s*(.+)"  # Verilator
        r"|(?:^|\n)\s*(?:error|Error):\s*(.+)"  # generic
        r"|(?:^|\n)\s*(\S+:\d+:\s*(?:error|syntax error).+))",  # Icarus/sv2v
    )
    m = error_re.search(error_output)
    if m:
        msg = next(g for g in m.groups() if g)
        return msg.strip()[:80]
    # Fallback: last non-empty line.
    for line in reversed(error_output.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith(("-", "=")):
            return stripped[:80]
    return ""
