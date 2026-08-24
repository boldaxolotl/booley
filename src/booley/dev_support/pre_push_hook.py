#!/usr/bin/env python3
"""Git pre-push hook: block pushes whose outgoing commits leak or misattribute.

The commit-msg sanitizer covers ``git commit`` and ``git merge`` — but git
runs no message hook at all for ``git revert``/``git cherry-pick``, and
``--no-verify`` disables commit-msg entirely (F-17). This hook is the safety
net at the last interception point git offers: it scans every commit about to
leave the machine and refuses the push on any of these offenses:

1. **Banned phrases** in the message, either identity, a changed tracked path,
   or a changed symlink target.
2. **An identity outside the allowlist**, when ``[stealth] allowed_authors``
   is configured. commit-msg cannot do this check: git hands that hook only
   the message file, so ``git commit --author='someone <else@local>'`` sails
   straight past it. Push time is the first place both identities are
   readable, and it is also the only place that catches a fabricated author
   arriving via rebase, cherry-pick, or ``--no-verify``.
3. **A repository-visible path into the project-state directory**, including
   a symlink whose committed target resolves there.

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
from dataclasses import dataclass
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
_MAX_SYMLINK_TARGET_BYTES = 64 * 1024
_SYMLINK_MODE = "120000"


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_id: str
    path: str


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


def _repository_root() -> Path | None:
    """Return the current repository root, or ``None`` when Git cannot."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _guard_project_dir(repository_root: Path) -> Path | None:
    """Resolve project state without requiring Booley in a vendored hook."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured:
        return Path(configured).resolve()
    try:
        from booley.runtime.project_dir import resolve_checkout_project_dir

        return resolve_checkout_project_dir(repository_root).resolve()
    except (ImportError, FileNotFoundError):
        hook_dir = Path(__file__).resolve().parent
        return hook_dir.parent if hook_dir.name == "hooks" else None


def _changed_tree_entries(sha: str) -> list[_TreeEntry] | None:
    """Changed non-deleted entries in *sha*, read from committed trees."""
    try:
        result = subprocess.run(
            [
                "git",
                "diff-tree",
                "--root",
                "-m",
                "-r",
                "--no-commit-id",
                "--no-renames",
                "--raw",
                "-z",
                sha,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return None
    entries: list[_TreeEntry] = []
    for offset in range(0, len(fields), 2):
        metadata = fields[offset].decode("ascii", errors="replace").split()
        if len(metadata) != 5 or not metadata[0].startswith(":"):
            return None
        mode, object_id = metadata[1], metadata[3]
        if mode != "000000":
            path = fields[offset + 1].decode("utf-8", errors="replace")
            entries.append(_TreeEntry(mode, object_id, path))
    return entries


def _symlink_target(entry: _TreeEntry) -> tuple[str | None, str | None]:
    """Return a committed symlink target and an inspection error, if any."""
    try:
        size_result = subprocess.run(
            ["git", "cat-file", "-s", entry.object_id],
            capture_output=True,
            text=True,
            encoding="ascii",
            timeout=10,
            check=False,
        )
        size = int(size_result.stdout.strip()) if size_result.returncode == 0 else -1
        if size < 0 or size > _MAX_SYMLINK_TARGET_BYTES:
            return None, f"cannot safely inspect symlink target: {entry.path}"
        blob_result = subprocess.run(
            ["git", "cat-file", "blob", entry.object_id],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, f"cannot inspect symlink target: {entry.path}"
    if blob_result.returncode != 0:
        return None, f"cannot inspect symlink target: {entry.path}"
    return blob_result.stdout.decode("utf-8", errors="replace"), None


def _inside_project_state(path: Path, project_dir: Path | None) -> bool:
    if project_dir is None:
        return False
    # Normalize ``..`` without following the current worktree's symlinks; the
    # committed target above, not mutable checkout state, is authoritative.
    normalized = Path(os.path.normpath(path.absolute()))
    state = Path(os.path.normpath(project_dir.absolute()))
    return normalized == state or state in normalized.parents


def _tree_offenses(sha: str, repository_root: Path, project_dir: Path | None) -> list[str]:
    entries = _changed_tree_entries(sha)
    if entries is None:
        return ["cannot inspect changed tracked paths"]

    offenses: list[str] = []
    for entry in entries:
        path_leaks = find_banned(entry.path)
        tracked_path = repository_root / entry.path
        if path_leaks:
            offenses.append(
                f"tracked path has banned terms ({', '.join(sorted(set(path_leaks)))}): "
                f"{entry.path}"
            )
        if _inside_project_state(tracked_path, project_dir):
            offenses.append(f"tracked path exposes project state: {entry.path}")
        if entry.mode != _SYMLINK_MODE:
            continue
        target, error = _symlink_target(entry)
        if error:
            offenses.append(error)
            continue
        assert target is not None
        target_leaks = find_banned(target)
        target_path = tracked_path.parent / target
        if target_leaks:
            offenses.append(
                f"symlink target has banned terms ({', '.join(sorted(set(target_leaks)))}): "
                f"{entry.path} -> {target}"
            )
        if _inside_project_state(target_path, project_dir):
            offenses.append(f"symlink target exposes project state: {entry.path} -> {target}")
    return list(dict.fromkeys(offenses))


def _commit_offenses(
    sha: str,
    allowlist: list[str],
    *,
    repository_root: Path | None = None,
    project_dir: Path | None = None,
) -> list[str]:
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
    root = repository_root or _repository_root()
    if root is None:
        offenses.append("cannot resolve repository root to inspect tracked metadata")
    else:
        state = project_dir if project_dir is not None else _guard_project_dir(root)
        offenses.extend(_tree_offenses(sha, root, state))
    return offenses


def main() -> int:
    if os.environ.get("BOOLEY_SKIP_PUSH_GUARD"):
        print("pre-push: leak guard skipped (BOOLEY_SKIP_PUSH_GUARD set)", file=sys.stderr)
        return 0
    if not stealth_enabled():
        return 0

    allowlist = allowed_authors()
    repository_root = _repository_root()
    if repository_root is None:
        print(
            "ERROR: push blocked: leak guard could not resolve the repository root.",
            file=sys.stderr,
        )
        return 1
    project_dir = _guard_project_dir(repository_root)

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
            offenses = _commit_offenses(
                sha,
                allowlist,
                repository_root=repository_root,
                project_dir=project_dir,
            )
            if offenses:
                offenders.append((sha, offenses))

    if not offenders:
        return 0

    print("ERROR: push blocked by leak-guard pre-push hook.", file=sys.stderr)
    print(
        "Outgoing commit(s) expose stealth metadata, carry banned terms, or have "
        "an author/committer identity that is not on the allowlist:",
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
