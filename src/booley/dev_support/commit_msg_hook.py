#!/usr/bin/env python3
"""Git commit-msg hook: reject attribution and banned terms, then validate.

The hook never writes the message file. It reads it, and either accepts it
untouched or rejects the commit with a diagnostic that names every problem and
how to retry with the same text.

1. Reject recognized machine-attribution footers.
2. Reject banned terms in the subject and body, listing each match with its
   location. Lines Git treats as comments (the comment prefix at column 0) and
   everything below the scissors line are not part of the message and are not
   checked; a term on a comment-prefixed line that Git keeps (``git commit -m``
   keeps ``#`` lines) is left to the pre-push hook.
3. Validate the subject convention and body-length cap, which
   ``BOOLEY_SKIP_COMMIT_VALIDATION`` skips. Attribution footers and banned
   terms are rejected even with that variable set.

Project Initialization packages this module into the Project Git-hook
bundle used by the RTL repository and its linked worktrees.

**Rejecting beats rewriting.** The hook used to replace banned terms with a
placeholder in place. That changed what the author wrote, produced scopes such
as ``fix(redacted)``, and made the committed history differ from the text the
author had reviewed. A rejection costs one retry and loses nothing.
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

from commit_msg_utils import find_banned, stealth_enabled

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
    # authored sentence remains ordinary prose and is not attribution.
    footer = next((line for line in reversed(lines) if line.strip()), "")
    match = _GENERATED_WITH_LINE_RE.fullmatch(footer)
    return bool(match and _payload_names_protected_identity(match.group("payload"), project_root))


def _git_output(project_root: Path | None, *args: str) -> tuple[bool, str]:
    """Run ``git`` in *project_root* and return (succeeded, stdout)."""
    from validate_commit_msg import run_command

    argv = ["git", *(["-C", str(project_root)] if project_root else []), *args]
    run = run_command(argv)
    return run.ok, run.stdout


def _comment_prefix(project_root: Path | None) -> str:
    """Return Git's comment prefix: ``core.commentChar``/``commentString``, else ``#``.

    Git treats the two keys as one setting where the last one set wins, and
    ``auto`` means "pick a character the message does not use", which cannot
    be known here, so it maps to ``#``.
    """
    ok, out = _git_output(project_root, "config", "--get-regexp", r"^core\.comment(char|string)$")
    if not ok or not out.strip():
        return "#"
    value = out.strip().splitlines()[-1].partition(" ")[2].strip()
    return "#" if value in ("", "auto") else value


def message_lines(raw: str, prefix: str) -> list[tuple[int, str]]:
    """Return ``(file line number, text)`` for the lines Git keeps in the message.

    Drops the scissors line and everything below it, and lines that start with
    *prefix* at column 0, as ``git commit`` does.
    """
    scissors = f"{prefix} ------------------------ >8 ------------------------"
    kept: list[tuple[int, str]] = []
    for number, line in enumerate(raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if line.startswith(scissors):
            break
        if not line.startswith(prefix):
            kept.append((number, line))
    return kept


def _subject_index(lines: list[tuple[int, str]]) -> int | None:
    """Return the index of the first nonblank kept line, if any."""
    return next((i for i, (_, text) in enumerate(lines) if text.strip()), None)


def banned_matches(
    lines: list[tuple[int, str]], subject_index: int | None, project_root: Path | None
) -> list[tuple[str, str]]:
    """Return ``(term, location)`` once per term per kept line."""
    matches: list[tuple[str, str]] = []
    for index, (number, text) in enumerate(lines):
        location = "in the subject" if index == subject_index else f"on line {number}"
        matches.extend((term, location) for term in dict.fromkeys(find_banned(text, project_root)))
    return matches


def _is_merge(msg_file: Path, project_root: Path | None) -> bool:
    """Return whether this commit concludes a merge."""
    if msg_file.name == "MERGE_MSG":
        return True
    return _git_output(project_root, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0]


def _print_kept_and_retry(msg_file: Path) -> None:
    """Say where the rejected message was kept and how to retry with it."""
    path = msg_file.resolve()
    print(f'Your message was kept unchanged in "{path}".', file=sys.stderr)
    print(
        "To retry with the same text, rerun your git commit command with the same "
        f'options (-a, --amend, paths) and replace -m/-F with: -v -e -F "{path}"',
        file=sys.stderr,
    )


def _print_banned_rejection(
    matches: list[tuple[str, str]],
    other_errors: list[str],
    msg_file: Path,
    *,
    merge: bool,
) -> None:
    """Print the banned-term rejection with every match and both ways out."""
    print(
        "commit-msg: Stealth Mode rejected this commit message; it contains banned terms:",
        file=sys.stderr,
    )
    for term, location in matches:
        print(f"  - {term!r} {location}", file=sys.stderr)
    for error in other_errors:
        print(f"  - {error}", file=sys.stderr)
    print(
        "Reword the message, or, if a term is an ordinary name in this design, "
        "remove it from [stealth] banned_words or banned_substrings in the "
        "project config TOML.",
        file=sys.stderr,
    )
    if merge:
        print(
            "This is a merge commit: finish it with a message of your own, "
            'git commit -m "<message>" concludes the merge, or pass '
            '-m "<message>" to git merge.',
            file=sys.stderr,
        )
    _print_kept_and_retry(msg_file)


def _check_message(msg_file: Path, raw: str, project_root: Path | None) -> int:
    """Return the hook exit code for one message, printing any rejection."""
    from validate_commit_msg import convention_errors

    lines = message_lines(raw, _comment_prefix(project_root))
    subject_index = _subject_index(lines)
    body = "\n".join(text for _, text in lines[(subject_index or 0) + 1 :])

    # Attribution first, and without echoing the footer's identity.
    if _has_machine_attribution(body, project_root):
        print(
            "commit-msg: attribution footer is not allowed in stealth mode; "
            "remove the footer and retry the commit",
            file=sys.stderr,
        )
        _print_kept_and_retry(msg_file)
        return 1

    matches = banned_matches(lines, subject_index, project_root)
    skip_convention = bool(os.environ.get("BOOLEY_SKIP_COMMIT_VALIDATION"))
    errors: list[str] = []
    if not skip_convention:
        text = "\n".join(text for _, text in lines[subject_index or 0 :])
        errors = convention_errors(text, project_root=project_root)

    if matches:
        _print_banned_rejection(matches, errors, msg_file, merge=_is_merge(msg_file, project_root))
        return 1
    if errors:
        print("Commit message validation FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "  (set BOOLEY_SKIP_COMMIT_VALIDATION=1 to skip this convention check "
            "for one commit; banned terms and attribution footers are still rejected)",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("commit-msg hook: no message file", file=sys.stderr)
        return 1

    from validate_commit_msg import _current_repo_root

    project_root = _current_repo_root()

    # Stealth mode is opt-in ([stealth] enabled = true). When off, this hook is
    # a no-op, even if a prior setup left it installed. Ticket worktrees are set
    # up fresh so the install is already gated; this makes the flag
    # authoritative at runtime too.
    if not stealth_enabled(project_root):
        return 0

    msg_file = Path(sys.argv[1])
    if not msg_file.is_file():
        print("commit-msg hook: message file not found", file=sys.stderr)
        return 1

    raw = msg_file.read_text(encoding="utf-8", errors="replace")
    return _check_message(msg_file, raw, project_root)


if __name__ == "__main__":
    sys.exit(main())
