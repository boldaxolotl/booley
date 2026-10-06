#!/bin/bash
# WorktreeCreate: creates an outer git worktree for later Python setup.
# Reads JSON from stdin, outputs worktree absolute path to stdout.
# All diagnostic messages go to stderr so they don't pollute stdout.
#
# Input JSON fields:
#   name         (required) single safe path component; the worktree is
#                created at <cwd>/.booley_project/worktrees/<name>.
#   cwd          (required) root of the Git repository to branch from.
#   branch_ref   (optional) existing refs/heads/... branch to attach; without
#                it the worktree starts on a detached HEAD at the current HEAD.
#   on_existing  (optional) policy when the destination already exists:
#                  "refuse"  (default) exit 1 without touching anything when
#                            the destination is an existing path, a registered
#                            worktree, or a half-created worktree.
#                  "replace" treat the destination as a stale leftover:
#                            force-remove the registered worktree and delete
#                            the directory before creating a fresh one. Only
#                            Ticket setup, which owns its per-Ticket
#                            worktrees, passes this policy.
#
# Shipped as runtime package data (booley/runtime/worktree_create.sh); the
# Ticket setup stage and `booley worktree new` both invoke it.

set -e

# Resolve a working Python (F-7): the container ships python3, but a Windows
# host (Git Bash) often has only python.exe — plus a Microsoft Store alias
# that LOOKS like python3 yet exits non-zero with a Store nag, so each
# candidate must actually run, not merely resolve on PATH. BOOLEY_PYTHON lets
# a caller (e.g. the Python harness) pin the exact interpreter. Held as an
# array so both a spaced path and the two-word `py -3` invoke cleanly.
PY=()
if [ -n "${BOOLEY_PYTHON:-}" ] && "$BOOLEY_PYTHON" -c '' >/dev/null 2>&1; then
    PY=("$BOOLEY_PYTHON")
else
    for _cand in python3 python; do
        if "$_cand" -c '' >/dev/null 2>&1; then PY=("$_cand"); break; fi
    done
    if [ ${#PY[@]} -eq 0 ] && py -3 -c '' >/dev/null 2>&1; then PY=(py -3); fi
fi
if [ ${#PY[@]} -eq 0 ]; then
    echo "ERROR: no usable Python found (tried \$BOOLEY_PYTHON, python3, python, py -3)" >&2
    exit 1
fi

# Parse input JSON (use Python instead of jq for portability)
INPUT=$(cat)
NAME=$(echo "$INPUT" | "${PY[@]}" -c "import sys,json; print(json.load(sys.stdin)['name'])")
CWD=$(echo "$INPUT" | "${PY[@]}" -c "import sys,json; print(json.load(sys.stdin)['cwd'])")
BRANCH_REF=$(echo "$INPUT" | "${PY[@]}" -c "import sys,json; value=json.load(sys.stdin).get('branch_ref', ''); isinstance(value, str) or sys.exit('ERROR: branch_ref must be a string'); print(value)")
ON_EXISTING=$(echo "$INPUT" | "${PY[@]}" -c "import sys,json; value=json.load(sys.stdin).get('on_existing', 'refuse'); value in ('refuse', 'replace') or sys.exit('ERROR: on_existing must be \"refuse\" or \"replace\"'); print(value)")

if [[ ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: worktree name must be a single safe path component: $NAME" >&2
    exit 1
fi

# Python's os.getcwd() returns Windows-style paths (C:\...) which break
# tar's -C flag — tar interprets the colon as a remote host separator.
# Convert to POSIX (/c/...) on MSYS2/Cygwin; no-op elsewhere.
if command -v cygpath >/dev/null 2>&1; then
    CWD_MIXED=$(cygpath -m "$CWD")
    if [[ "$CWD_MIXED" =~ ^([A-Za-z]):/(.*)$ ]]; then
        CWD="/${BASH_REMATCH[1],,}/${BASH_REMATCH[2]}"
    else
        CWD=$(cygpath -u "$CWD")
    fi
fi

# The hook payload is external input.  Refuse to create state (or later run
# destructive cleanup) relative to an arbitrary writable directory: CWD must
# be the root of the Git repository whose worktree we are about to create.
if ! CWD_REAL=$(cd "$CWD" 2>/dev/null && pwd -P); then
    echo "ERROR: worktree cwd does not exist or is not accessible: $CWD" >&2
    exit 1
fi
if [ ! -e "$CWD_REAL/.git" ]; then
    echo "ERROR: worktree cwd must be the Git repository root: $CWD" >&2
    exit 1
fi
if ! git -C "$CWD_REAL" rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: worktree cwd is not a Git repository: $CWD" >&2
    exit 1
fi
CWD="$CWD_REAL"

EXPLICIT_BRANCH=""
if [ -n "$BRANCH_REF" ]; then
    if [[ "$BRANCH_REF" != refs/heads/* ]] || ! git -C "$CWD" check-ref-format "$BRANCH_REF"; then
        echo "ERROR: branch_ref must be a valid refs/heads/... ref: $BRANCH_REF" >&2
        exit 1
    fi
    if ! git -C "$CWD" rev-parse --verify --quiet "${BRANCH_REF}^{commit}" >/dev/null; then
        echo "ERROR: branch_ref does not exist as a local branch: $BRANCH_REF" >&2
        exit 1
    fi
    EXPLICIT_BRANCH="${BRANCH_REF#refs/heads/}"
fi

WORKTREE_DIR="$CWD/.booley_project/worktrees/$NAME"
CREATING_MARKER="$WORKTREE_DIR/.creating"

# Keep this worktree's .core copies out of FuseSoC's recursive --cores-root
# scan. Each worktree is a full checkout carrying the project's .core files
# (same VLNV as /work); without this marker FuseSoC's scan lets a stale copy
# shadow the repo-root source and silently build the wrong RTL. `booley init`
# also drops this marker, but a worktree can be created against a project that
# predates the fix — so guarantee it here whenever a worktree exists. The
# marker suppresses .core DISCOVERY only; source files a baseline fileset
# references under .booley_project/ are still read.
mkdir -p "$CWD/.booley_project"
FUSESOC_IGNORE_MARKER="$CWD/.booley_project/FUSESOC_IGNORE"
if [ ! -f "$FUSESOC_IGNORE_MARKER" ]; then
    printf '# Booley state dir: keep transient worktree .core copies out of FuseSoC/Booley core discovery.\n' \
        > "$FUSESOC_IGNORE_MARKER" 2>/dev/null || true
fi

# Cleanup trap: if we fail mid-build AND we're the one that started creating
# this worktree (marker file present), remove the half-built worktree so it
# doesn't leave an orphan that confuses later runs. Concurrent runners are
# gated by the per-worktree flock below. _OWNS_WORKTREE flips to true only
# after THIS invocation created the worktree, so an early failure can never
# delete a half-created worktree that belongs to someone else.
_OWNS_WORKTREE=false
cleanup_on_error() {
    local rc=$?
    if [ "$_OWNS_WORKTREE" = true ] && [ -f "$CREATING_MARKER" ]; then
        echo "ERROR: worktree_create.sh failed (rc=$rc) — cleaning up partial worktree" >&2
        git -C "$CWD" worktree remove "$WORKTREE_DIR" --force 2>/dev/null || true
        rm -rf "$WORKTREE_DIR"
        git -C "$CWD" worktree prune 2>/dev/null || true
    fi
    exit $rc
}
trap cleanup_on_error ERR

# Serialize concurrent invocations on the SAME worktree name. Different
# worktrees still run in parallel (lockfile is per-name). Prevents racing
# runners from blowing each other's directories away mid-build.
LOCK_DIR="$CWD/.booley_project/worktrees/.locks"
mkdir -p "$LOCK_DIR"
LOCKFILE="$LOCK_DIR/$NAME.lock"

_flock_works=false
if command -v flock >/dev/null 2>&1; then
    # Probe: MSYS2 flock can't use inherited FDs (Bad file descriptor).
    _probe_lock="$LOCK_DIR/.flock_probe.$$"
    if (exec 200>"$_probe_lock" && flock -xn 200) 2>/dev/null; then
        _flock_works=true
    fi
    rm -f "$_probe_lock"
fi

if [ "$_flock_works" = true ]; then
    # Open FD 200 on the lockfile, take an exclusive lock. Released on exit.
    exec 200>"$LOCKFILE"
    flock -x 200
    # Stamp the owner PID *after* acquisition so the lock GC can reap a lock
    # left by a killed process instantly, instead of waiting out the 5-minute
    # age fallback and logging a misleading "no PID, exceeded max age" (F-54).
    # Unlinking the file here is not safe (a waiter may already hold an fd on
    # it), so teardown removes it once the worktree itself is gone.
    echo $$ >&200
else
    # Fallback for systems without flock (rare on MSYS2/Linux but safe):
    # mkdir-based lock with retry. Directory creation is atomic.
    LOCK_MKDIR="$LOCK_DIR/$NAME.mkdir.lock"

    # Break stale locks left by dead processes.
    # Check PID file first (instant, reliable); fall back to 5-min age check.
    if [ -d "$LOCK_MKDIR" ]; then
        _break_lock=false
        if [ -f "$LOCK_MKDIR/pid" ]; then
            _owner_pid=$(cat "$LOCK_MKDIR/pid" 2>/dev/null)
            if [ -n "$_owner_pid" ] && ! kill -0 "$_owner_pid" 2>/dev/null; then
                _break_lock=true
                echo "WARNING: Breaking mkdir lock owned by dead PID $_owner_pid: $LOCK_MKDIR" >&2
            fi
        fi
        if [ "$_break_lock" = false ]; then
            _stale=$(find "$LOCK_MKDIR" -maxdepth 0 -mmin +5 2>/dev/null)
            if [ -n "$_stale" ]; then
                _break_lock=true
                echo "WARNING: Breaking stale mkdir lock (older than 5 min): $LOCK_MKDIR" >&2
            fi
        fi
        if [ "$_break_lock" = true ]; then
            rm -rf "$LOCK_MKDIR"
        fi
    fi

    _lock_attempts=0
    while ! mkdir "$LOCK_MKDIR" 2>/dev/null; do
        _lock_attempts=$((_lock_attempts + 1))
        if [ $_lock_attempts -gt 600 ]; then
            echo "ERROR: failed to acquire mkdir lock on $LOCK_MKDIR after 10 min" >&2
            exit 1
        fi
        sleep 1
    done
    # Record owner PID so other processes can detect stale locks instantly.
    mkdir -p "$LOCK_MKDIR"
    echo $$ > "$LOCK_MKDIR/pid"
    # Release the mkdir lock on any exit (success or failure). Keep the
    # ERR trap (cleanup_on_error, set above) separate so it only fires on error.
    # Also clean the parent-git mkdir lock (set below) if we acquired it.
    trap 'rm -rf "$LOCK_MKDIR" 2>/dev/null || true; [ "${_PARENT_LOCK_HELD:-false}" = true ] && [ -n "${_PARENT_LOCK_MKDIR:-}" ] && rm -rf "$_PARENT_LOCK_MKDIR" 2>/dev/null || true' EXIT
fi

# Parent-git lock: serialises ONLY the sections that mutate the parent repo's
# .git/config and .git/worktrees/ metadata. Different worktree names race here
# because the per-name lock above doesn't cover the shared parent .git/config —
# concurrent `git worktree add` / `git config` calls collide on .git/config.lock
# and on Windows the loser exits 128 with "could not lock config file".
# Tar/copy work between the two critical sections still overlaps across workers.
_PARENT_LOCKFILE="$LOCK_DIR/_parent_git.lock"
_PARENT_LOCK_MKDIR=""
_PARENT_LOCK_HELD=false

_parent_lock_acquire() {
    if [ "$_flock_works" = true ]; then
        exec 201>"$_PARENT_LOCKFILE"
        flock -x 201
        _PARENT_LOCK_HELD=true
    else
        _PARENT_LOCK_MKDIR="$LOCK_DIR/_parent_git.mkdir.lock"
        # Break stale parent-lock left by a dead PID (matches per-name fallback).
        if [ -d "$_PARENT_LOCK_MKDIR" ] && [ -f "$_PARENT_LOCK_MKDIR/pid" ]; then
            _po=$(cat "$_PARENT_LOCK_MKDIR/pid" 2>/dev/null)
            if [ -n "$_po" ] && ! kill -0 "$_po" 2>/dev/null; then
                echo "WARNING: Breaking parent-git lock owned by dead PID $_po" >&2
                rm -rf "$_PARENT_LOCK_MKDIR"
            fi
        fi
        _pa=0
        while ! mkdir "$_PARENT_LOCK_MKDIR" 2>/dev/null; do
            _pa=$((_pa + 1))
            if [ "$_pa" -gt 600 ]; then
                echo "ERROR: failed to acquire parent-git lock after 10 min" >&2
                exit 1
            fi
            sleep 1
        done
        _PARENT_LOCK_HELD=true
        mkdir -p "$_PARENT_LOCK_MKDIR"
        echo $$ > "$_PARENT_LOCK_MKDIR/pid"
    fi
}

_parent_lock_release() {
    if [ "$_flock_works" = true ]; then
        # Closing FD 201 releases the flock; FD 200 (per-name) stays open.
        exec 201>&-
        _PARENT_LOCK_HELD=false
    elif [ "$_PARENT_LOCK_HELD" = true ] && [ -n "$_PARENT_LOCK_MKDIR" ]; then
        rm -rf "$_PARENT_LOCK_MKDIR" 2>/dev/null || true
        _PARENT_LOCK_HELD=false
        _PARENT_LOCK_MKDIR=""
    fi
}

# --- Critical section A: parent .git/config + .git/worktrees/ writes ---
# Take the parent-git lock around worktree-remove/prune/add so concurrent
# workers don't race on .git/config.lock. Released before the tar/copy work.
_parent_lock_acquire

# Refuse policy (the default): an existing destination may be an active
# worktree holding someone's work, so never delete it. Checked here, under
# the per-name creation lock, before `worktree prune` can forget a registered
# entry. Covers any existing path (directory, file, or dangling symlink,
# including a half-created worktree with its .creating marker) and a
# worktree Git still registers at that path even if its directory is gone.
if [ "$ON_EXISTING" = "refuse" ]; then
    _destination_problem=""
    if [ -e "$WORKTREE_DIR" ] || [ -L "$WORKTREE_DIR" ]; then
        _destination_problem="the path already exists"
    else
        # Git prints registered worktree paths in its own spelling: POSIX on
        # Linux/macOS, drive-letter mixed form (C:/...) under Git for Windows.
        _registered_forms=("$WORKTREE_DIR")
        if command -v cygpath >/dev/null 2>&1; then
            _registered_forms+=("$(cygpath -m "$WORKTREE_DIR")")
        fi
        _registered=$(git -C "$CWD" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' || true)
        for _form in "${_registered_forms[@]}"; do
            # Case-insensitive on purpose: Windows paths are, and a false
            # match only refuses (never deletes).
            if printf '%s\n' "$_registered" | grep -qixF -- "$_form"; then
                _destination_problem="a Git worktree is already registered there"
            fi
        done
    fi
    if [ -n "$_destination_problem" ]; then
        _parent_lock_release
        echo "ERROR: worktree destination is not free ($_destination_problem): $WORKTREE_DIR" >&2
        echo "ERROR: choose another name, or remove the old worktree yourself with 'git worktree remove' once its work is safe" >&2
        exit 1
    fi
fi

git -C "$CWD" worktree prune 2>/dev/null || true

# Replace policy: a same-name directory is a stale leftover from a previous
# Ticket run. This handles both broken leftovers (no .git file) and complete
# worktrees that weren't cleaned up (e.g. agent died mid-execution).
if [ "$ON_EXISTING" = "replace" ] && [ -d "$WORKTREE_DIR" ]; then
    echo "WARNING: Stale worktree directory detected at $WORKTREE_DIR — removing" >&2
    git -C "$CWD" worktree remove "$WORKTREE_DIR" --force 2>/dev/null || true
    rm -rf "$WORKTREE_DIR"
    git -C "$CWD" worktree prune 2>/dev/null || true
fi

# Check if name encodes a branch: "branch--description" convention.
# If NAME contains "--", the part before it is treated as a branch to check out.
TARGET_BRANCH=""
if [ -n "$EXPLICIT_BRANCH" ]; then
    TARGET_BRANCH="$EXPLICIT_BRANCH"
elif [[ "$NAME" == *--* ]]; then
    TARGET_BRANCH="${NAME%%--*}"
    echo "Branch detected in name: $TARGET_BRANCH" >&2
fi

# Ticket Baseline callers attach their exact existing branch. Other callers
# retain the generic detached-HEAD behavior and optional name convention.
WORKTREE_ADD_TARGET=(--detach)
if [ -n "$EXPLICIT_BRANCH" ]; then
    WORKTREE_ADD_TARGET=("$EXPLICIT_BRANCH")
fi
echo "Creating worktree: $WORKTREE_DIR" >&2
# stdout goes to stderr too: `git worktree add` prints "HEAD is now at ..."
# on stdout, and this script's contract is worktree path ONLY on stdout.
ADD_ERR="$LOCK_DIR/${NAME}.worktree-add.$$.err"
if ! git -C "$CWD" -c submodule.recurse=false worktree add "$WORKTREE_DIR" "${WORKTREE_ADD_TARGET[@]}" >&2 2>"$ADD_ERR"; then
    if [ "$ON_EXISTING" = "replace" ] && grep -qi "missing but already registered worktree" "$ADD_ERR"; then
        echo "WARNING: Worktree registered but missing; pruning and retrying" >&2
        git -C "$CWD" worktree remove "$WORKTREE_DIR" --force 2>/dev/null || true
        git -C "$CWD" worktree prune 2>/dev/null || true
        rm -rf "$WORKTREE_DIR"
        git -C "$CWD" -c submodule.recurse=false worktree add "$WORKTREE_DIR" "${WORKTREE_ADD_TARGET[@]}" >&2
    else
        cat "$ADD_ERR" >&2
        rm -f "$ADD_ERR"
        false
    fi
fi
rm -f "$ADD_ERR"

_parent_lock_release
# --- End critical section A ---

# Drop marker so cleanup_on_error knows this is a mid-build worktree.
# Removed at the end of the script once everything succeeded. From here on
# this invocation owns the new worktree, so the ERR trap may clean it up.
_OWNS_WORKTREE=true
touch "$CREATING_MARKER"

# If a target branch was specified, check it out
if [ -z "$EXPLICIT_BRANCH" ] && [ -n "$TARGET_BRANCH" ]; then
    # Branch selection precedes Python's offline submodule materialization.
    if git -C "$WORKTREE_DIR" rev-parse --verify "$TARGET_BRANCH" >/dev/null 2>&1; then
        echo "Checking out branch: $TARGET_BRANCH" >&2
        git -C "$WORKTREE_DIR" -c submodule.recurse=false checkout "$TARGET_BRANCH" >&2
    elif git -C "$WORKTREE_DIR" rev-parse --verify "origin/$TARGET_BRANCH" >/dev/null 2>&1; then
        echo "Checking out remote branch: origin/$TARGET_BRANCH" >&2
        git -C "$WORKTREE_DIR" -c submodule.recurse=false checkout -b "$TARGET_BRANCH" "origin/$TARGET_BRANCH" >&2
    else
        echo "WARNING: Branch '$TARGET_BRANCH' not found locally or on remote — staying on detached HEAD" >&2
    fi
fi

# Validate: .git must be a FILE (gitdir pointer), not a directory
if [ ! -f "$WORKTREE_DIR/.git" ]; then
    echo "ERROR: Worktree creation failed — .git file missing at $WORKTREE_DIR" >&2
    exit 1
fi

# Resolve the linked worktree's private gitdir once and use it for host-side
# worktree commands. Some sandbox runs leave the shared repo config with
# core.worktree=/work, which is valid there but makes host-side
# `git -C "$WORKTREE_DIR" checkout` fail with "must be run in a work tree".
WORKTREE_GIT_DIR=$(sed 's/^gitdir: //' "$WORKTREE_DIR/.git")
if [[ "$WORKTREE_GIT_DIR" != /* && ! "$WORKTREE_GIT_DIR" =~ ^[A-Za-z]: ]]; then
    WORKTREE_GIT_DIR="$WORKTREE_DIR/$WORKTREE_GIT_DIR"
fi

git_wt() {
    git --git-dir="$WORKTREE_GIT_DIR" --work-tree="$WORKTREE_DIR" "$@"
}

# Resolve project dir: env var -> sibling .booley_project/ -> legacy project/
PIPELINE_DIR="$CWD/.booley"
if [ -n "${BOOLEY_PROJECT_DIR:-}" ]; then
    BOOLEY_PROJECT_DIR_RESOLVED="$BOOLEY_PROJECT_DIR"
elif [ -d "$CWD/.booley_project" ]; then
    BOOLEY_PROJECT_DIR_RESOLVED="$CWD/.booley_project"
else
    BOOLEY_PROJECT_DIR_RESOLVED="$PIPELINE_DIR/project"
fi

# --- Critical section B: more parent .git/config writes ---
# Enable per-worktree config and keep all worktree settings out of the shared
# parent .git/config. Re-acquire the parent-git lock around this cluster because
# enabling extensions.worktreeConfig still mutates the shared config.
_parent_lock_acquire

# Disable automatic submodule recursion. Python materializes exact gitlinks
# after final branch selection without remotes or shared .git pointers.
git -C "$CWD" config extensions.worktreeConfig true
RELATIVE_WORKTREE_DIR=$("${PY[@]}" -c 'import os,sys; print(os.path.relpath(os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])).replace(os.sep, "/"))' "$WORKTREE_DIR" "$WORKTREE_GIT_DIR")
git_wt config --worktree core.worktree "$RELATIVE_WORKTREE_DIR"
git_wt config --worktree submodule.recurse false
git_wt config --worktree diff.ignoreSubmodules all
git_wt config --worktree status.submoduleSummary false
echo "Submodule recursion disabled in worktree git config" >&2

# Force LF line endings in the worktree.  The host (Windows) has
# core.autocrlf=true, but Docker containers do not — any git command the
# agent runs inside Docker would see every CRLF file as "modified".
# Setting autocrlf=false here makes host and container agree on LF.
git_wt config --worktree core.autocrlf false
git_wt checkout -f >&2
echo "Line endings normalised to LF (core.autocrlf=false)" >&2

if [ "$(git -C "$CWD" config --bool --get worktree.useRelativePaths 2>/dev/null || true)" = "true" ]; then
    WORKTREE_POINTER=$(sed 's/^gitdir: //' "$WORKTREE_DIR/.git")
    REVERSE_POINTER=$(cat "$WORKTREE_GIT_DIR/gitdir")
    CONFIGURED_WORKTREE=$(git_wt config --worktree --get core.worktree)
    if [[ "$WORKTREE_POINTER" = /* || "$WORKTREE_POINTER" =~ ^[A-Za-z]: ]] ||
       [[ "$REVERSE_POINTER" = /* || "$REVERSE_POINTER" =~ ^[A-Za-z]: ]] ||
       [[ "$CONFIGURED_WORKTREE" = /* || "$CONFIGURED_WORKTREE" =~ ^[A-Za-z]: ]]; then
        echo "ERROR: relative worktree policy produced non-portable metadata" >&2
        exit 1
    fi
fi

# Apply the same validated [agent.git] policy used by Interactive Mode. Older
# layouts may keep booley.toml under .booley/, but pipeline.toml is never read.
IDENTITY_PROJECT_DIR="$BOOLEY_PROJECT_DIR_RESOLVED"
if [ ! -f "$IDENTITY_PROJECT_DIR/booley.toml" ] && [ -f "$PIPELINE_DIR/booley.toml" ]; then
    IDENTITY_PROJECT_DIR="$PIPELINE_DIR"
fi
BOOLEY_PACKAGE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
PYTHONPATH="$BOOLEY_PACKAGE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    BOOLEY_PROJECT_DIR="$IDENTITY_PROJECT_DIR" BOOLEY_GIT_CHECKOUT="$WORKTREE_DIR" \
    "${PY[@]}" -m booley.runtime.incontainer_git_identity --ticket

_parent_lock_release
# --- End critical section B ---

# Copy .booley/ into the worktree (scripts, templates, etc.).
# Scripts like run_iverilog_sim.py derive project_root from __file__, so they must
# exist inside the worktree for path resolution to work correctly.
if [ -d "$CWD/.booley" ]; then
    echo "Copying .booley/ into worktree..." >&2
    mkdir -p "$WORKTREE_DIR/.booley"
    tar -C "$CWD/.booley" \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='worktrees' \
        --exclude='tmp' \
        --exclude='target' \
        --exclude='.pytest_cache' \
        --exclude='.ruff_cache' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        -cf - . | tar -C "$WORKTREE_DIR/.booley" -xf -
    echo ".booley/ copied ($(du -sh "$WORKTREE_DIR/.booley" 2>/dev/null | cut -f1))" >&2
fi

# Copy .booley_project/ into worktree if it exists as a sibling (convention-based discovery).
# Live state stays behind: Ticket boards/logs/locks, Goal state under goals/
# (records, locks, and history; committed Goal history reaches the worktree
# through Git only), and per-session runtime state under runtime/sessions/.
# Those two patterns start with ./ so tar matches only the top-level members
# (the archive is built from "."); a nested directory named goals is copied.
if [ -d "$CWD/.booley_project" ]; then
    echo "Copying .booley_project/ into worktree..." >&2
    mkdir -p "$WORKTREE_DIR/.booley_project"
    tar -C "$CWD/.booley_project" --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='baselines' \
        --exclude='baseline_codex' \
        --exclude='full_run' \
        --exclude='full_run_*' \
        --exclude='.interactive_logs' \
        --exclude='orphaned_after_*' \
        --exclude='worktrees' \
        --exclude='worktrees_disabled*' \
        --exclude='tickets/board' \
        --exclude='tickets/logs' \
        --exclude='tickets/locks' \
        --exclude='./goals' \
        --exclude='./runtime/sessions' \
        --exclude='.locks' \
        --exclude='tmp' \
        --exclude='eval' \
        --exclude='eval_*' \
        --exclude='eval_debug*' \
        --exclude='eval_tmp' \
        --exclude='*_disabled_for_*' \
        --exclude='stale_workspace_snapshots_*' \
        -cf - . | tar -C "$WORKTREE_DIR/.booley_project" -xf -
    echo ".booley_project/ copied ($(du -sh "$WORKTREE_DIR/.booley_project" 2>/dev/null | cut -f1))" >&2
fi

# Root-level quarantine markers are commonly untracked by design, so git
# worktree add cannot carry them. Preserve the same FuseSoC scan boundary in
# every ticket checkout.
if [ -f "$CWD/FUSESOC_IGNORE" ]; then
    cp "$CWD/FUSESOC_IGNORE" "$WORKTREE_DIR/FUSESOC_IGNORE"
fi

# Project-specific setup (sim symlinks, git hooks, DPI build, etc.) is
# handled by the post-setup hook in .booley_project/hooks/.
# See stage_01_setup.py for hook invocation.

# Build succeeded — remove the mid-build marker so ERR trap won't nuke
# the worktree on any later non-zero exit (there shouldn't be one after this,
# but defensive cleanup is cheap).
rm -f "$CREATING_MARKER"

# Release fallback mkdir-lock (flock lock releases automatically when FD 200 closes)
if [ -n "${LOCK_MKDIR:-}" ]; then
    rm -rf "$LOCK_MKDIR" 2>/dev/null || true
fi

# Print the worktree path for diagnostic / standalone use.
# The pipeline ignores stdout (it constructs the path natively).
echo "$WORKTREE_DIR"
