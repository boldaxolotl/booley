"""The report snippet of one Mutant: the part of its simulator output that explains its status.

A detected Mutant's snippet starts at the failure (or timeout) line of the run
that detected it, so the Snippet column of ``mutation-results.md`` shows why
the Mutant died. A surviving Mutant has no failure to show; its snippet is the
tail of its output.
"""

from __future__ import annotations

import re

from booley.flows.sim.result import has_simulation_timeout

#: Characters a snippet keeps; the manifest carries a hint, ``log_path`` the full log.
SNIPPET_MAX_CHARS = 200
#: Non-empty lines kept after the anchor line.
_CONTEXT_LINES = 2
#: Lines of a run's tail when it has no anchor line.
_TAIL_LINES = 5

#: A failure word. The lookarounds keep it out of paths, flags, file names and
#: identifiers (``/ip/error-correction/``, ``sva_errors``, ``-Werror``,
#: ``error.log``, ``assertion_tb``) and out of zero counts (``failed=0``) while
#: ``%Error:``, ``$error``, ``FATAL:``, ``AssertionError:``, ``test_bad failed``
#: and ``mismatch:`` match. It is not ``first_native_failure``'s substring scan
#: (``sim/adapter_transport.py``) on purpose: that scan hits exactly those
#: false positives, and changing it would alter the Simulation Flow's output.
_FAILURE_WORD_RE = re.compile(
    r"(?<![\w/\\.-])(?:failed|fatal|error|assert(?:ion)?s?(?:error)?|mismatch)"
    r"(?![\w/\\-]|\.\w)(?!\s*[=:]\s*0\b)",
    re.IGNORECASE,
)
#: Header lines every run-half prints before its simulator output; they name
#: the command and working directory, which may contain failure words.
_HEADER_PREFIXES = ("CWD:", "CMD:", "FILTER:", "TESTCASE:")
#: The banner line ahead of them (``[cocotb simulation: <module> on <tool>]``);
#: it names the top module or test module, which may contain a failure word.
_BANNER_RE = re.compile(r"^\[\w+ simulation: ")


def tail(text: str, max_lines: int) -> str:
    """Return the last *max_lines* of *text* as a single string."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def tail_snippet(output: str) -> str:
    """Return the last lines of *output*, capped: a survivor's snippet."""
    return tail(output, _TAIL_LINES)[-SNIPPET_MAX_CHARS:]


def _is_failure_line(line: str) -> bool:
    """True for a line that reports a failure rather than echoing a command."""
    stripped = line.strip()
    if stripped.startswith(_HEADER_PREFIXES) or _BANNER_RE.match(stripped):
        return False
    return bool(_FAILURE_WORD_RE.search(line))


def _first_failure(lines: list[str], killing_test: str) -> int | None:
    """Index of the line where *killing_test* failed, else of the first failure line."""
    failures = [(i, line) for i, line in enumerate(lines) if _is_failure_line(line)]
    if killing_test:
        killer = re.compile(rf"(?<!\w){re.escape(killing_test)}\s+failed\b")
        for index, line in failures:
            if killer.search(line):
                return index
    return failures[0][0] if failures else None


def _anchored(lines: list[str], anchor: int) -> str:
    """Return the collapsed anchor line plus up to two following non-empty lines."""
    kept = [" ".join(lines[anchor].split())]
    for line in lines[anchor + 1 :]:
        collapsed = " ".join(line.split())
        if collapsed:
            kept.append(collapsed)
        if len(kept) > _CONTEXT_LINES:
            break
    return "\n".join(kept)[:SNIPPET_MAX_CHARS]


def detection_snippet(
    test_name: str, output: str, *, timed_out: bool, killing_test: str = ""
) -> str:
    """Return the snippet of a Mutant detected by run *test_name*, whose output is *output*.

    A timed-out run starts at its own timeout line; when the Mutation Flow
    stopped the run itself there is none, so a message naming the test stands
    in for it. Any other detection starts at the line where *killing_test* (the
    test the verdict names; a cocotb batch holds many) failed, else at the
    run's first failure line, or shows the run's tail when it printed none.
    """
    lines = output.splitlines()
    if timed_out:
        anchor = next((i for i, line in enumerate(lines) if has_simulation_timeout(line)), None)
        if anchor is None:
            stopped = f"{test_name}: ran past its time limit and was stopped"
            return "\n".join(filter(None, (stopped, tail(output, _TAIL_LINES))))[
                :SNIPPET_MAX_CHARS
            ]
    else:
        anchor = _first_failure(lines, killing_test)
        if anchor is None:
            return tail_snippet(output)
    return _anchored(lines, anchor)
