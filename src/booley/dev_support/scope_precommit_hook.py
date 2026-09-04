#!/usr/bin/env python3
"""Git pre-commit hook: reject out-of-scope and forbidden commits.

Installed by the harness's setup step into worktree .git/hooks/.
Reads scope from .scope.json in the worktree root (written by setup).

Harness bookkeeping is rejected outright: an agent
that can rewrite development state or booley.toml can make a red run look green.

Exit 0 = allow commit; exit 1 = reject with diagnostic message.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path

try:
    from booley.ticket_board.contract_path_policy import (
        is_static_contract_path,
        normalize_contract_path,
    )
except ModuleNotFoundError:
    from contract_path_policy import is_static_contract_path, normalize_contract_path

try:
    from booley.dev_support.commit_msg_utils import source_checkout_policy_owner
except ModuleNotFoundError:
    from commit_msg_utils import source_checkout_policy_owner

# Intentional duplication of harness.scope_policy._FORBIDDEN_* -- this hook must
# run standalone inside a worktree, without Booley on sys.path.  Keep in sync.
_FORBIDDEN_PREFIXES = (".booley_project/", ".booley/", ".git/")
_FORBIDDEN_CARVE_OUTS = (
    ".booley_project/adapters/",
    ".booley_project/docs/",
)
_FORBIDDEN_EXACT = frozenset({".scope.json"})


def _load_scope(wt: Path) -> list[str] | None:
    """Load scope from .scope.json. Returns None if file missing."""
    scope_file = wt / ".scope.json"
    if not scope_file.exists():
        return None
    try:
        data = json.loads(scope_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # .scope.json is external input: only a JSON object with a string-list
    # "scope" is a usable scope. Anything else (non-object, non-list scope, or
    # a bare string that would iterate character-by-character in _matches_scope)
    # is treated like a missing file — no scope to compare against, so no
    # warning; the forbidden check below is scope-independent and still runs.
    if not isinstance(data, dict):
        return None
    scope = data.get("scope")
    if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
        return None
    return scope


def _load_contract_controls(wt: Path) -> set[str]:
    """Load exact sealed control paths; malformed policy fails closed to static rules."""
    try:
        data = json.loads((wt / ".scope.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    controls = data.get("contract_control")
    if not isinstance(controls, list) or not all(isinstance(path, str) for path in controls):
        return set()
    return {path.replace("\\", "/").removeprefix("./") for path in controls}


def _staged_files() -> list[str]:
    """Get list of files in the git index (staged for commit).

    Includes deletions (D): removing an out-of-scope file is just as much a
    deviation as adding/modifying one, and triage wants to see both.

    ``-z`` matters for the forbidden check: without it git C-quotes any path
    holding a space or a non-ASCII byte, and the leading quote would defeat
    every prefix test in ``_is_forbidden`` -- letting a bookkeeping file
    through purely because of how it is spelled.
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.split("\0") if f.strip()]


def _is_forbidden(filepath: str, contract_controls: set[str] | None = None) -> bool:
    """True for harness bookkeeping the agent must never commit."""
    normalized = normalize_contract_path(filepath)
    contract_path = normalized in (contract_controls or set()) or is_static_contract_path(
        normalized
    )
    if contract_path:
        return True
    if normalized in _FORBIDDEN_EXACT:
        return True
    if any(normalized.startswith(c) for c in _FORBIDDEN_CARVE_OUTS):
        return False
    return any(normalized.startswith(p) for p in _FORBIDDEN_PREFIXES)


def _matches_scope(filepath: str, scope: list[str]) -> bool:
    """Check if filepath matches any scope entry (literal or glob).

    Intentional duplication of harness.git_utils.scope_matches_file — this
    hook must run standalone without harness imports.
    """
    for entry in scope:
        if any(c in entry for c in ("*", "?", "[")):
            if fnmatch.fnmatch(filepath, entry):
                return True
        # A non-glob entry is an exact path or a directory prefix: `rtl/verilog`
        # and `rtl/verilog/` both own everything beneath rtl/verilog/ (F-14).
        elif filepath == entry or filepath.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def _reject_forbidden(forbidden: list[str]) -> int:
    """Print the hard-block diagnostic for harness-owned paths."""
    print(
        "ERROR: Commit blocked — these files belong to the harness or Acceptance Basis.",
        file=sys.stderr,
    )
    for f in forbidden:
        print(f"  - {f}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Development state, criteria, ticket files, and Target/control-plane inputs are "
        "the record your run is graded against. Unstage these and commit the rest; "
        "request a Target contract revision when the sealed recipe must change.",
        file=sys.stderr,
    )
    return 1


def _reject_out_of_scope(out_of_scope: list[str], scope: list[str]) -> int:
    """Block a commit that escaped the ticket's declared file scope."""
    print("ERROR: Commit blocked — files are outside the ticket scope.", file=sys.stderr)
    print(f"Ticket scope: {scope}", file=sys.stderr)
    print("Outside it:", file=sys.stderr)
    for f in out_of_scope:
        print(f"  - {f}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Update the ticket scope through triage, or unstage these paths and "
        "commit only the authorized files.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    wt = Path.cwd()
    if source_checkout_policy_owner(wt):
        return 0
    scope = _load_scope(wt)
    contract_controls = _load_contract_controls(wt)

    staged = _staged_files()
    if not staged:
        return 0

    # Scope-independent: runs even when .scope.json is missing or malformed.
    forbidden = [f for f in staged if _is_forbidden(f, contract_controls)]
    if forbidden:
        return _reject_forbidden(forbidden)

    if scope is None:
        return 0

    # The ["*"] unknown-scope sentinel grants no ownership: a ticket that named
    # nothing owns nothing, so everything it touched needs human triage.
    wildcard = scope == ["*"]
    out_of_scope = [f for f in staged if wildcard or not _matches_scope(f, scope)]
    if out_of_scope:
        return _reject_out_of_scope(out_of_scope, scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
