#!/usr/bin/env python3
"""Git commit-msg hook: sanitize then validate commit messages.

1. Sanitize: redact banned phrases from the subject *and* the body — both must
   survive, so banned phrases are substituted in place rather than the text
   being dropped. Attribution trailers (``Co-Authored-By:``, the "Generated
   with" footer) are the one thing removed outright: redacting one leaves a
   mangled trailer that still smells of its origin, and it carries no authorial
   content worth keeping.
2. Validate via validate_commit_msg.validate_message().

Installed by the setup stage (harness/setup/workspace.py) into .git/hooks/commit-msg.

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

from commit_msg_utils import redact_banned, stealth_enabled

# Body lines dropped outright rather than redacted: git trailers and footers
# whose whole purpose is to attribute the commit to an assistant. Redacting one
# yields debris ("redacted: redacted <noreply@...>") that is both ugly and still
# recognizably an attribution trailer, and unlike a prose line it carries no
# content the author would miss.
# Only these two shapes are DROPPED. Both are machine-written attributions with
# no authorial content, and both would redact into debris
# ("redacted: redacted <noreply@...>") that still reads as an attribution.
#
# Nothing else is dropped, deliberately. An earlier draft also dropped any
# "Generated with <agent>" line — but "generated" is itself on the banned list,
# so that rule fired on *every* such line, honest prose included ("Generated
# with care by the whole team"), silently deleting a real sentence. Everything
# outside these two patterns is redacted in place instead: a mangled word is
# recoverable, a deleted line is not.
_ATTRIBUTION_LINE_RE = re.compile(
    r"""^\s*(?:
          co-authored-by\s*:          # the git trailer itself
        | \U0001F916                  # 🤖 — the "Generated with ..." footer marker
    )""",
    re.IGNORECASE | re.VERBOSE,
)


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


def sanitize_body(body: str, project_root: Path | None = None) -> str:
    """Redact a commit body in place, dropping only attribution trailers.

    Returns the body with no leading/trailing blank lines, so the caller can
    re-attach it under exactly one blank separator line (git's convention)
    regardless of how the author spaced the original.
    """
    kept = [ln for ln in body.split("\n") if not _ATTRIBUTION_LINE_RE.match(ln)]
    return _trim_blank_edges("\n".join(redact_banned(ln, project_root) for ln in kept))


def sanitize_message(msg: str, project_root: Path | None = None) -> str:
    """Sanitize a commit message: redact banned content, drop comment lines.

    Both subject and body are redacted in place. This is the real leak surface:
    the message lands in history verbatim, so a bare *validation* check would
    only reject the commit rather than scrub it.
    """
    raw_subject, body = split_subject_body(msg)
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
    # The body is no longer dropped — it is redacted and kept — but the author
    # still deserves to know their wording changed. Compare against the body
    # with only its blank edges trimmed: that is what sanitize_body() would
    # return if it had nothing to redact, so any difference is a real edit and
    # re-spacing alone never trips the notice.
    if _trim_blank_edges(raw_body) != sanitize_body(raw_body, project_root):
        print(
            "commit-msg: stealth-mode redaction rewrote the commit body "
            "(banned phrases substituted, attribution trailers removed); "
            "the body itself is kept",
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
    sanitized = sanitize_message(raw, project_root)
    msg_file.write_text(sanitized, encoding="utf-8")
    _notify_if_redacted(raw, sanitized, project_root)

    # Validate the sanitized message
    from validate_commit_msg import validate_message

    errors = validate_message(sanitized, project_root=project_root)
    if errors:
        # Opt-out for human/upstream-style commits on non-Booley IP (SETUP-10):
        # skip only the type(scope): summary CONVENTION check. Sanitization
        # above already ran, so the IP-leak scrub is NOT bypassed — unlike
        # `git commit --no-verify`, which disables the whole hook.
        if os.environ.get("BOOLEY_SKIP_COMMIT_VALIDATION"):
            print(
                "commit-msg: convention check skipped "
                "(BOOLEY_SKIP_COMMIT_VALIDATION set); message still sanitized",
                file=sys.stderr,
            )
            return 0
        print("Commit message validation FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "  (set BOOLEY_SKIP_COMMIT_VALIDATION=1 to skip this convention "
            "check for one commit; sanitization still applies)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
