#!/usr/bin/env python3
"""Parse a captured Xcelium (xrun) simulation log for internal incubation.

This module contains pure parsing functions over captured logs and never
launches a subprocess. Xcelium is not publicly eligible in Booley; these
helpers are retained only to incubate a possible future Session Runtime
integration:

  * :func:`evaluate_xcelium_log` — verdict fields from log text + exit code.
    The TB verdict sentinels (``[SIM_RESULT] PASSED/FAILED``) are
    simulator-agnostic, so :func:`sim_result.parse_sim_verdict` does the primary
    classification; xrun-specific error counting comes from
    :func:`sim_result.count_sva_errors_xcelium`.
  * :func:`reemit_xcelium_summary` — appends the ``[SIM_SUMMARY]`` sentinel the
    verdict layer scrapes (the xcelium mirror of ``sim_edam.reemit_sim_summary``).
  * ``python -m booley.flows.sim.backends.experimental.xcelium --parse-log <xrun.log>`` — the offline
    entry point: parse a hand-carried log, print the summary, write
    ``result.json`` for internal calibration and debugging.

Error patterns are frozen against real xrun(64) 21.03-s001 logs captured in the
Phase B calibration loop (ADR 0025, 2026-07); ``extract_vrfc_warnings`` is
Xilinx-xvlog-specific and unused here.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.flows.run_log import write_run_log
from booley.flows.sim.result import (
    SIM_SUMMARY_PREFIX,
    count_sva_errors_xcelium,
    format_summary,
    parse_sim_verdict,
    write_result_json,
)

# Substrings that mark a log line as an xrun error worth surfacing as
# ``first_error`` in result.json (EDA tool diagnostics or TB failure wording).
_FIRST_ERROR_MARKERS = (
    "*E,",
    "*F,",
    "FAILED",
    "Fatal:",
    "Error!",
    "Mismatch",
    "ERROR:",
)


@dataclass
class XceliumVerdict:
    """Verdict fields parsed from one captured xrun log."""

    passed: bool
    sva_errors: int
    inconclusive: bool
    first_error: str = ""


def evaluate_xcelium_log(text: str, exit_code: int) -> XceliumVerdict:
    """Classify a captured xrun log into Booley's structured sim verdict.

    Same decision table as the Icarus/Verilator run-halves: the TB sentinel is
    authoritative when present; otherwise a non-zero exit or any counted error
    is a FAIL, and a clean sentinel-less exit is *inconclusive* (never a silent
    pass). The sentinel is scanned over the full text — an xrun build preamble
    can push it out of a fixed tail window (same rationale as
    ``sim_edam.reemit_sim_summary``).
    """
    verdict = parse_sim_verdict(text)  # full-scan default; preamble can't bury sentinel
    sva_errors = count_sva_errors_xcelium(text)
    if verdict is True:
        passed, inconclusive = sva_errors == 0, False
    elif verdict is False or exit_code != 0 or sva_errors > 0:
        passed, inconclusive = False, False
    else:
        passed, inconclusive = False, True

    first_error = ""
    if not passed and not inconclusive:
        for line in text.splitlines():
            if any(marker in line for marker in _FIRST_ERROR_MARKERS):
                first_error = line.strip()
                break
    return XceliumVerdict(
        passed=passed,
        sva_errors=sva_errors,
        inconclusive=inconclusive,
        first_error=first_error,
    )


def reemit_xcelium_summary(output: str, exit_code: int) -> str:
    """Append the ``[SIM_SUMMARY]`` sentinel to a raw xrun log (ADR 0019).

    Given raw parser input with no structured summary, Booley synthesizes the
    summary line here. Idempotent like
    ``sim_edam.reemit_sim_summary``: an existing authoritative summary (a TB
    that prints its own, or a re-parse) is never clobbered.
    """
    if any(ln.startswith(SIM_SUMMARY_PREFIX) for ln in output.splitlines()):
        return output
    v = evaluate_xcelium_log(output, exit_code)
    summary = format_summary(v.passed, v.sva_errors, inconclusive=v.inconclusive)
    sep = "" if (not output or output.endswith("\n")) else "\n"
    return f"{output}{sep}{summary}"


def parse_log_file(
    log_path: Path,
    *,
    exit_code: int = 0,
    work_dir: Path | None = None,
) -> XceliumVerdict:
    """Offline mode: parse a captured xrun log, print + persist the verdict.

    Prints the ``[SIM_SUMMARY]`` sentinel and a human verdict line to stdout;
    writes ``result.json`` into *work_dir* (default: the log's directory) via
    :func:`sim_result.write_result_json` so the captured-log flow produces the
    same artifacts as a live run.
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")
    v = evaluate_xcelium_log(text, exit_code)

    print(format_summary(v.passed, v.sva_errors, inconclusive=v.inconclusive))
    if v.inconclusive:
        print(f"\nxcelium sim INCONCLUSIVE (rc={exit_code}, no sentinel)")
    elif v.passed:
        print(f"\nxcelium sim PASSED (rc={exit_code})")
    elif v.sva_errors > 0:
        print(f"\nxcelium sim FAILED ({v.sva_errors} error/assertion lines)")
    else:
        # Not passed and no counted assertions → either a FAIL sentinel (which
        # can accompany a clean xrun exit) or a nonzero rc. Cite rc only when
        # it is actually nonzero to avoid the confusing "(rc=0)".
        reason = f"rc={exit_code}" if exit_code else "fail sentinel matched"
        print(f"\nxcelium sim FAILED ({reason})")

    out_dir = work_dir if work_dir is not None else log_path.parent
    write_result_json(
        out_dir,
        v.passed,
        v.sva_errors,
        v.first_error,
        exit_code,
        inconclusive=v.inconclusive,
    )
    # Persist the full raw log next to result.json on pass AND fail: the
    # xrun log includes compile + elaboration + sim output, and result.json
    # only carries a 500-char first_error — run.log is what survives
    # MCP-level stdout truncation (the filename is a summary contract).
    write_run_log(out_dir, text)
    return v


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Parse a captured Xcelium (xrun) log into Booley's sim verdict",
    )
    p.add_argument(
        "--parse-log",
        required=True,
        metavar="XRUN_LOG",
        help="captured xrun log file (e.g. xrun.log or the broker's returned log)",
    )
    p.add_argument(
        "--exit-code",
        type=int,
        default=0,
        help="the xrun/make exit code observed on the EDA host (default: 0)",
    )
    p.add_argument(
        "--work-dir",
        default=None,
        help="where to write result.json (default: the log's directory)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path = Path(args.parse_log)
    if not log_path.is_file():
        print(f"ERROR: log file not found: {log_path}", file=sys.stderr)
        return 2
    v = parse_log_file(
        log_path,
        exit_code=args.exit_code,
        work_dir=Path(args.work_dir) if args.work_dir else None,
    )
    return 0 if v.passed else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
