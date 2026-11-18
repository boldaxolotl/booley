"""Selection of the informative lines of failing command output.

Shared by Doctor and its diagnostic owners so the excerpt rule is stated once.
Pure text in, text out; it knows nothing of how the lines are rendered.
"""

from __future__ import annotations

__all__ = ["output_excerpt_lines"]

# Substrings that mark the informative failure lines in EDA/fusesoc output. The
# reason for a fusesoc parse error is printed on the line AFTER
# "Parse error. Ignoring file X:", so a first-line-only excerpt dropped it.
_OUTPUT_ERROR_MARKERS = (
    "error",
    "must be array",
    "ignoring file",
    "could not",
    "cannot",
    "traceback",
    "failed",
    "exception",
    "not found",
    "requires",
)


def output_excerpt_lines(text: str, max_lines: int = 8) -> list[str]:
    """Return the informative slice of multi-line command output (indented).

    A first-line-only excerpt truncated multi-line diagnostics — notably a
    fusesoc parse error whose reason (``... must be array``) lands on the *next*
    line. When any line looks like an error, take from the first such line
    through the line after the last one (to catch the trailing reason); otherwise
    fall back to the head of the output. Capped at *max_lines* with a "… N more"
    note so the report dir is still the full record.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    flagged = [
        i for i, ln in enumerate(lines) if any(m in ln.lower() for m in _OUTPUT_ERROR_MARKERS)
    ]
    if flagged and max_lines > 1:
        selected = lines[flagged[0] : flagged[-1] + 2][:max_lines]
    else:
        selected = lines[:max_lines]
    excerpt = [f"    {ln}" for ln in selected]
    hidden = len(lines) - len(selected)
    if hidden > 0:
        excerpt.append(f"    … ({hidden} more line(s); see the report dir)")
    return excerpt
