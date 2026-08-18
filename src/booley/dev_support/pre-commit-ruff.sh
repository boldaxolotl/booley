#!/bin/bash
# Git pre-commit hook: block commits with Ruff violations in staged .py files.
#
# Installed by booley init into .git/hooks/pre-commit.
# Also usable standalone: copy to any repo's .git/hooks/pre-commit.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Get staged .py files
STAGED=$(git diff --cached --name-only --diff-filter=ACMR -- '*.py')
if [ -z "$STAGED" ]; then
    exit 0
fi

# Prefer the repo's own virtualenv over whatever happens to be on PATH: a
# ruff in ~/.local/bin shadows the venv for anyone who hasn't activated it,
# which is how this repo ended up linting against three different versions.
RUFF=""
for CANDIDATE in "$REPO_ROOT/.venv/bin/ruff" "$REPO_ROOT/.venv/Scripts/ruff.exe"; do
    if [ -x "$CANDIDATE" ]; then
        RUFF="$CANDIDATE"
        break
    fi
done
if [ -z "$RUFF" ]; then
    RUFF="$(command -v ruff 2>/dev/null || true)"
fi
if [ -z "$RUFF" ]; then
    echo "WARNING: ruff not found on PATH, skipping pre-commit lint check"
    exit 0
fi

# If pyproject.toml pins an exact ruff, say so when the resolved binary differs.
# Warn rather than fail: this hook also ships into projects whose pin (if any)
# is none of Booley's business, and a version skew is a lint-fidelity problem,
# not a reason to block a commit outright.
PINNED=$(grep -oE '"ruff==[0-9]+\.[0-9]+\.[0-9]+"' "$REPO_ROOT/pyproject.toml" 2>/dev/null \
    | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
if [ -n "$PINNED" ]; then
    FOUND=$("$RUFF" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [ -n "$FOUND" ] && [ "$FOUND" != "$PINNED" ]; then
        echo "WARNING: $RUFF is $FOUND but pyproject.toml pins ruff==$PINNED;" >&2
        echo "         local results may not match CI. Fix: pip install ruff==$PINNED" >&2
    fi
fi

# Run ruff check on staged files only
echo "$STAGED" | xargs "$RUFF" check --force-exclude
exit $?
