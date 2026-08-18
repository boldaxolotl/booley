"""Pre-processor that wraps raw ``path:line[:col]`` tokens in backticks.

Raw lint/sim stdout doesn't use backticks, so the renderer wouldn't see
anything to make clickable. We do the smallest possible thing: scan each
EDA-tool-output line for ``path:line[:col]`` tokens and, when the path
actually resolves under project or worktree root, wrap it in backticks.
The renderer then applies its normal "backticked path → clickable" rule
on a uniform input shape.

False-positive guard
--------------------
The regex itself is permissive (``foo.bar:42`` matches). The resolver
must confirm the path exists; otherwise the substitution is skipped.
Both ``clk`` (no extension) and ``foo.bar`` (no file) survive untouched.

Sandbox path mapping
--------------------
``/work/rtl/fifo.sv:42`` from an EDA tool running inside the sandbox is
stripped to ``rtl/fifo.sv:42`` before resolution; the wrapper emits the
stripped form so the click resolver doesn't need to know about the
container layout.
"""

from __future__ import annotations

import re

from .links import LinkContext

# A reasonably tight path token. Requirements:
#   * starts with a non-word, non-slash, non-backtick character (or BOL)
#     so we don't pick up substrings inside identifiers or URLs.
#   * extension is required (the dot disambiguates from C identifiers).
#   * the ``:line`` segment is required — that's what we're after.
# Examples that match: ``rtl/fifo.sv:42``, ``rtl/fifo.sv:42:7``,
# ``./tb_fifo.sv:1``.
# Examples that don't: ``clk``, ``foo.bar``, ``http://x:8080``.
_PATH_LINE_RE = re.compile(
    r"(?<![`/\w])"
    r"(?P<path>[\w./\\-]+\.[a-zA-Z][a-zA-Z0-9]*)"
    r":(?P<line>\d+)"
    r"(?::(?P<col>\d+))?",
)


def wrap_paths_in_backticks(line: str, ctx: LinkContext) -> str:
    """Wrap any ``path:line[:col]`` tokens in *line* that resolve to files.

    The renderer then turns each wrapped token into a clickable span
    via its standard backticked-path rule.

    Already-backticked tokens are left alone (the negative lookbehind
    above prevents the regex from matching inside an existing pair).
    """
    if not line:
        return line

    def _sub(match: re.Match[str]) -> str:
        raw_path = match.group("path")
        line_num = match.group("line")
        # Strip container mount prefix before resolution.
        candidate = ctx.strip_sandbox_prefix(raw_path)
        if not ctx.resolves_under_roots(candidate):
            return match.group(0)
        return f"`{candidate}:{line_num}`"

    return _PATH_LINE_RE.sub(_sub, line)
