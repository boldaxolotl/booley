"""The managed line-ending rule earlier Booley versions wrote, read by Goal Finish."""

import re
from collections.abc import Iterator

GITATTRIBUTES_RULE = "* text=auto eol=lf"


def _policy_lines(content: bytes) -> Iterator[bytes]:
    # Git attr.c uses this exact blank set; VT/FF are pattern bytes, not whitespace.
    for raw_line in content.split(b"\n"):
        line = raw_line.strip(b" \t\r\n")
        if line and not line.startswith(b"#"):
            yield line


def has_managed_attributes(content: bytes, *, replayable: bool = False) -> bool:
    """Recognize the managed rule, optionally requiring no other policy content."""
    lines = list(_policy_lines(content))
    rule = GITATTRIBUTES_RULE.encode()
    if not replayable:
        return rule in lines
    return lines.count(rule) == 1 and not local_policy_owned(
        b"\n".join(line for line in lines if line != rule)
    )


def local_policy_owned(content: bytes) -> bool:
    """Whether the content holds attribute policy beyond export-ignore/export-subst."""
    unrelated = {b"export-ignore", b"-export-ignore", b"export-subst", b"-export-subst"}
    for line in _policy_lines(content):
        fields = re.split(rb"[ \t\r\n]+", line)
        if len(fields) < 2 or any(field not in unrelated for field in fields[1:]):
            return True
    return False
