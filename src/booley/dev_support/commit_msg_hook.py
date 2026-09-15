#!/usr/bin/env python3
"""Git commit-msg hook: reject attribution, sanitize, then validate.

1. Reject recognized machine-attribution footers from the raw message without
   rewriting it, so the author can remove the footer and retry.
2. Sanitize ordinary content: redact banned phrases from the subject *and* the
   body in place so authored rationale survives.
3. Validate via validate_commit_msg.validate_message().

Project Initialization vendors this module for the Project hook; Ticket
worktrees receive the same source through harness/setup/workspace.py.

**Bodies are kept.** They used to be truncated away on every non-merge commit
("single-line messages only"), which quietly destroyed authored work: a long
commit body explaining a port — every design decision, the QoR baseline — was
dropped on the floor, and the notice saying so scrolled past in a wall of git
output (taxi port, F-11). Stealth's job is to keep agent and EDA-tool names out of the
history, not to keep *rationale* out of it; a redacted body leaks exactly as
little as no body at all, and a history of terse one-liners is arguably the
*less* human-looking outcome. Losing work to a lossy default is worse than
either.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Ensure dev_support/ is on sys.path so imports resolve when called as a hook.
_DEV_SUPPORT_DIR = str(Path(__file__).resolve().parent)
if _DEV_SUPPORT_DIR not in sys.path:
    sys.path.insert(0, _DEV_SUPPORT_DIR)

from commit_msg_utils import find_banned, redact_banned, stealth_enabled


class AttributionPolicyError(ValueError):
    """A commit message contains a machine-attribution footer."""


# These existing structural forms are unambiguous attribution markers and are
# rejected regardless of Project vocabulary. They used to be silently dropped,
# which made the hook's policy depend on footer spelling and destroyed the raw
# message the author needed to correct.
_STRUCTURAL_ATTRIBUTION_LINE_RE = re.compile(
    r"""^\s*(?:
          co-authored-by\s*:          # the git trailer itself
        | \U0001F916                  # 🤖 — the "Generated with ..." footer marker
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_GENERATED_WITH_LINE_RE = re.compile(
    r"^\s*generated\s+with\s+(?P<payload>.+?)\s*$",
    re.IGNORECASE,
)

_MARKDOWN_LINK_RE = re.compile(r"^\[(?P<label>[^]]+)\]\([^)]+\)$")


def split_subject_body(msg: str) -> tuple[str, str]:
    """Normalize line endings, drop git's ``#`` comment lines, split subject/body.

    The subject is returned *unredacted* so callers can tell whether
    sanitization actually changed it.
    """
    msg = msg.replace("\r\n", "\n").replace("\r", "\n")
    non_comment = "\n".join(ln for ln in msg.split("\n") if not ln.lstrip().startswith("#"))
    parts = non_comment.split("\n", 1)
    return parts[0].strip(), (parts[1] if len(parts) > 1 else "")


def _trim_blank_edges(body: str) -> str:
    """Drop leading and trailing blank lines from *body*."""
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _payload_names_protected_identity(
    payload: str,
    project_root: Path | None = None,
) -> bool:
    """Return whether an attribution payload is exactly one protected name."""
    link = _MARKDOWN_LINK_RE.fullmatch(payload)
    visible_name = (link.group("label") if link else payload).strip()
    return any(
        visible_name.casefold() == protected_name.casefold()
        for protected_name in find_banned(visible_name, project_root)
    )


def _has_machine_attribution(body: str, project_root: Path | None = None) -> bool:
    """Return whether the body contains a recognized attribution footer."""
    lines = body.split("\n")
    for line in lines:
        if _STRUCTURAL_ATTRIBUTION_LINE_RE.match(line):
            return True

    # Plain "Generated with ..." text is ambiguous, so only the final nonblank
    # body line has footer shape. Its visible payload must be exactly one
    # protected vocabulary entry: merely mentioning a protected term inside an
    # authored sentence remains ordinary prose and takes the redaction path.
    footer = next((line for line in reversed(lines) if line.strip()), "")
    match = _GENERATED_WITH_LINE_RE.fullmatch(footer)
    return bool(match and _payload_names_protected_identity(match.group("payload"), project_root))


def sanitize_body(body: str, project_root: Path | None = None) -> str:
    """Redact a commit body in place without deleting authored lines.

    Returns the body with no leading/trailing blank lines, so the caller can
    re-attach it under exactly one blank separator line (git's convention)
    regardless of how the author spaced the original.
    """
    return _trim_blank_edges(
        "\n".join(redact_banned(line, project_root) for line in body.split("\n"))
    )


def sanitize_message(msg: str, project_root: Path | None = None) -> str:
    """Reject attribution or sanitize a commit message in place.

    Raises :class:`AttributionPolicyError` before redaction when the raw body
    contains recognized attribution. Otherwise both subject and body are
    redacted in place and git comment lines are dropped.
    """
    raw_subject, body = split_subject_body(msg)
    if _has_machine_attribution(body, project_root):
        raise AttributionPolicyError
    subject = redact_banned(raw_subject, project_root)
    clean_body = sanitize_body(body, project_root)
    if clean_body:
        return f"{subject}\n\n{clean_body}\n"
    return subject + "\n"


def _notify_if_redacted(
    raw: str,
    sanitized: str,
    project_root: Path | None = None,
) -> None:
    """Tell the committer when sanitization rewrote their message (F-8/F-15).

    Redaction is stealth-by-design but was also stealth-from-the-author: a
    subject like ``feat(booley): ...`` silently landed as ``feat(redacted): ...``,
    so a scope name turned into a useless one with nothing to notice — very
    confusing on a first encounter. The commit still proceeds — this only makes
    the substitution visible and names its cause (stealth mode). Prints the
    *sanitized* text; echoing the banned phrase back would defeat the point.
    """
    raw_subject, raw_body = split_subject_body(raw)
    sanitized_subject, _sanitized_body = split_subject_body(sanitized)
    changed = False
    if sanitized_subject != raw_subject:
        print(
            f"commit-msg: stealth-mode redaction rewrote the subject -> {sanitized_subject}",
            file=sys.stderr,
        )
        changed = True
    # Checked independently of the subject: an `elif` here meant a redacted
    # subject swallowed the body notice, so body edits happened without a word.
    # The body is redacted and kept, but the author still deserves to know their
    # wording changed. Compare against the body
    # with only its blank edges trimmed: that is what sanitize_body() would
    # return if it had nothing to redact, so any difference is a real edit and
    # re-spacing alone never trips the notice.
    if _trim_blank_edges(raw_body) != sanitize_body(raw_body, project_root):
        print(
            "commit-msg: stealth-mode redaction rewrote the commit body "
            "(banned phrases substituted); the body itself is kept",
            file=sys.stderr,
        )
        changed = True
    if changed:
        # Names the knob but not the config file: the file is <project
        # dir>/booley.toml, and spelling that out would print a banned word —
        # the exact thing these notices promise not to echo back.
        print(
            "commit-msg: stealth mode did this — opt out with [stealth] "
            "enabled = false in the project config TOML to keep messages verbatim",
            file=sys.stderr,
        )


def _validate_sanitized_message(sanitized: str, project_root: Path | None) -> int:
    """Validate sanitized text, honoring the validation-only escape hatch."""
    from validate_commit_msg import validate_message

    errors = validate_message(sanitized, project_root=project_root)
    if not errors:
        return 0

    # Opt-out for human/upstream-style commits on non-Booley IP (SETUP-10):
    # skip only the configured validation checks. Attribution rejection and
    # sanitization have already run and cannot be bypassed here.
    if os.environ.get("BOOLEY_SKIP_COMMIT_VALIDATION"):
        print(
            "commit-msg: convention check skipped "
            "(BOOLEY_SKIP_COMMIT_VALIDATION set); message still sanitized",
            file=sys.stderr,
        )
        return 0
    print("Commit message validation FAILED:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    print(
        "  (set BOOLEY_SKIP_COMMIT_VALIDATION=1 to skip this convention "
        "check for one commit; sanitization still applies)",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    if len(sys.argv) < 2:
        print("commit-msg hook: no message file", file=sys.stderr)
        return 1

    from validate_commit_msg import _current_repo_root

    project_root = _current_repo_root()

    # Stealth mode is opt-out ([stealth] enabled = false). When off, this hook —
    # sanitizer and convention validator both — is a no-op, even if a prior
    # setup left it installed. Ticket worktrees are set up fresh so the install
    # is already gated; this makes the flag authoritative at runtime too.
    if not stealth_enabled(project_root):
        return 0

    msg_file = Path(sys.argv[1])
    if not msg_file.is_file():
        print("commit-msg hook: message file not found", file=sys.stderr)
        return 1

    raw = msg_file.read_text(encoding="utf-8", errors="replace")
    try:
        sanitized = sanitize_message(raw, project_root)
    except AttributionPolicyError:
        print(
            "commit-msg: attribution footer is not allowed in stealth mode; "
            "remove the footer and retry the commit",
            file=sys.stderr,
        )
        return 1
    msg_file.write_text(sanitized, encoding="utf-8")
    _notify_if_redacted(raw, sanitized, project_root)

    # Validate the sanitized, attribution-free message.
    return _validate_sanitized_message(sanitized, project_root)


if __name__ == "__main__":
    sys.exit(main())
