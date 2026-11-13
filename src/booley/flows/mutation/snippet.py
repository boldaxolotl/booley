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

#: A failure word. The lookarounds keep it out of paths, flags and identifiers
#: (``/ip/error-correction/``, ``sva_errors``, ``-Werror``) while ``%Error:``,
#: ``$error``, ``FATAL:``, ``AssertionError:``, ``test_bad failed`` and
#: ``mismatch:`` match.
_FAILURE_WORD_RE = re.compile(
    r"(?<![\w/\\.-])(?:failed|fatal|error|assert\w*|mismatch)(?![\w/\\-])",
    re.IGNORECASE,
)
#: Header lines every run-half prints before its simulator output; they name
#: the command and working directory, which may contain failure words.
_HEADER_PREFIXES = ("CWD:", "CMD:", "FILTER:", "TESTCASE:")


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
    return not line.strip().startswith(_HEADER_PREFIXES) and bool(_FAILURE_WORD_RE.search(line))


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


def detection_snippet(test_name: str, output: str, *, timed_out: bool) -> str:
    """Return the snippet of a Mutant detected by the run *test_name* produced *output*.

    A timed-out run starts at its own timeout line; when the Mutation Flow
    stopped the run itself there is none, so a message naming the test stands
    in for it. Any other detection starts at the run's first failure line, or
    shows the run's tail when it printed none.
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
        anchor = next((i for i, line in enumerate(lines) if _is_failure_line(line)), None)
        if anchor is None:
            return tail_snippet(output)
    return _anchored(lines, anchor)
