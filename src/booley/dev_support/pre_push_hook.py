#!/usr/bin/env python3
"""Git pre-push hook: block pushes whose outgoing commits leak or misattribute.

The commit-msg sanitizer covers ``git commit`` and ``git merge`` — but git
runs no message hook at all for ``git revert``/``git cherry-pick``, and
``--no-verify`` disables commit-msg entirely (F-17). This hook is the safety
net at the last interception point git offers: it scans every commit about to
leave the machine — message, author, and committer — and refuses the push on
either of two offenses:

1. **Banned phrases** anywhere in the message or in either identity.
2. **An identity outside the allowlist**, when ``[stealth] allowed_authors``
   is configured. commit-msg cannot do this check: git hands that hook only
   the message file, so ``git commit --author='someone <else@local>'`` sails
   straight past it. Push time is the first place both identities are
   readable, and it is also the only place that catches a fabricated author
   arriving via rebase, cherry-pick, or ``--no-verify``.

Vendored into the project's hooks dir by ``booley init`` (Step 10b), beside
``commit_msg_utils.py``, so it resolves its imports by bare name with no
Booley source checkout. Escape hatch: ``BOOLEY_SKIP_PUSH_GUARD=1`` skips the
scan for one push (e.g. the first push of pre-existing upstream history that
predates the hook).

Exit 0 = allow push; exit 1 = reject with diagnostic naming the commits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Ensure the vendored hooks dir is on sys.path so imports resolve when called
# as a hook (mirrors commit_msg_hook.py).
_HOOK_DIR = str(Path(__file__).resolve().parent)
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)

from commit_msg_utils import allowed_authors, find_banned, identity_allowed, stealth_enabled

_ZERO_SHA_PREFIX = "0000000"

# A push of deep pre-Booley history (e.g. the first push of an imported repo)
# is not this hook's threat model; scanning thousands of upstream commits
# both slows the push and invites false positives. Scan a bounded window of
# the newest outgoing commits instead.
_MAX_COMMITS_SCANNED = 500


def _outgoing_commits(local_sha: str, remote_sha: str) -> list[str]:
    """SHAs about to be pushed for one ref, newest first (bounded)."""
    if remote_sha.startswith(_ZERO_SHA_PREFIX):
        # New remote branch: everything not already on some remote is outgoing.
        range_args = [local_sha, "--not", "--remotes"]
    else:
        range_args = [f"{remote_sha}..{local_sha}"]
    result = subprocess.run(
        ["git", "rev-list", f"--max-count={_MAX_COMMITS_SCANNED}", *range_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.split() if s]


# Identity fields first, one per line, then the message last — the message is
# the only multi-line field, so putting it at the end makes the split
# unambiguous without needing a separator that could occur inside a name.
_COMMIT_FORMAT = "%an%n%ae%n%cn%n%ce%n%B"
_IDENTITY_FIELDS = 4


def _commit_facts(sha: str) -> tuple[str, str, str, str, str] | None:
    """``(author_name, author_email, committer_name, committer_email, message)``.

    None when git cannot read the commit — the caller treats that as "nothing
    to report" rather than blocking a push on a git failure it cannot explain.
    """
    result = subprocess.run(
        ["git", "log", "-1", f"--format={_COMMIT_FORMAT}", sha],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.split("\n")
    if len(lines) < _IDENTITY_FIELDS + 1:
        return None
    author_name, author_email, committer_name, committer_email = lines[:_IDENTITY_FIELDS]
    message = "\n".join(lines[_IDENTITY_FIELDS:])
    return author_name, author_email, committer_name, committer_email, message


def _commit_offenses(sha: str, allowlist: list[str]) -> list[str]:
    """Human-readable reasons this commit must not be pushed (empty = fine)."""
    facts = _commit_facts(sha)
    if facts is None:
        return []
    author_name, author_email, committer_name, committer_email, message = facts

    offenses: list[str] = []
    scanned = f"{message}\n{author_name} <{author_email}>\n{committer_name} <{committer_email}>"
    leaks = find_banned(scanned)
    if leaks:
        offenses.append(f"banned terms: {', '.join(sorted(set(leaks)))}")

    # Both identities are checked, not just the author: a fabricated author
    # with the real committer (what `git commit --author=...` produces) and a
    # real author with a fabricated committer are the same problem seen from
    # two ends, and the allowlist is cheap enough to apply to both.
    for role, name, email in (
        ("author", author_name, author_email),
        ("committer", committer_name, committer_email),
    ):
        if not identity_allowed(name, email, allowlist):
            offenses.append(f"{role} not in [stealth] allowed_authors: {name} <{email}>")
    return offenses


def main() -> int:
    if os.environ.get("BOOLEY_SKIP_PUSH_GUARD"):
        print("pre-push: leak guard skipped (BOOLEY_SKIP_PUSH_GUARD set)", file=sys.stderr)
        return 0
    if not stealth_enabled():
        return 0

    allowlist = allowed_authors()

    offenders: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha.startswith(_ZERO_SHA_PREFIX):
            continue  # branch deletion — nothing outgoing
        for sha in _outgoing_commits(local_sha, remote_sha):
            if sha in seen:
                continue
            seen.add(sha)
            offenses = _commit_offenses(sha, allowlist)
            if offenses:
                offenders.append((sha, offenses))

    if not offenders:
        return 0

    print("ERROR: push blocked by leak-guard pre-push hook.", file=sys.stderr)
    print(
        "Outgoing commit(s) carry banned terms, or an author/committer identity "
        "that is not on the allowlist:",
        file=sys.stderr,
    )
    for sha, offenses in offenders:
        print(f"  - {sha[:12]}: {'; '.join(offenses)}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Fix: rewrite the offending commits (e.g. git rebase -i, or "
        "git commit --amend --reset-author to reclaim a misattributed one), "
        "or set BOOLEY_SKIP_PUSH_GUARD=1 to push anyway (e.g. pre-existing "
        "upstream history whose authors predate the allowlist).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
