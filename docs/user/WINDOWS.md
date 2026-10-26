# Windows first-run problems

The CLI runs **natively on Windows** (repo on `C:\...`, Windows Python), not
from inside WSL. Docker Desktop's WSL2 backend only hosts the containers.

This supported layout prioritizes normal Windows editor and Git access. The
checkout is bind-mounted into the Linux Sandbox, so metadata-heavy EDA
builds may perform differently from Linux-native storage. That performance is
still being qualified; a slow simulation should be reported with its phase
timings rather than attributed to the bind mount from total elapsed time alone.
A checkout hosted inside a user WSL distribution is an optional performance
experiment, not a setup requirement.

Eight first-run traps:

- **Git is older than 2.37.2.** Upgrade Git for Windows from
  [git-scm.com](https://git-scm.com/downloads/win), open a new terminal, and
  rerun `booley init`. Older releases can exhaust their small pool of temporary
  names when Booley stages a large checkout for line-ending repair, so bootstrap
  stops early and names the installed and required versions.
- **`python`/`py` open the Microsoft Store instead of running.** A fresh
  Windows has no real interpreter, only Store aliases. Install one first:
  `winget install Python.Python.3.13`.
- **`docker` isn't found even though Docker Desktop is installed.** Its CLI
  joins `PATH` only after the app has started: launch Docker Desktop, then open
  a **new** terminal before `booley init`. Init treats a missing CLI or stopped
  daemon as fatal and exits before creating or changing project files.
- **The container sees a fully modified tree / phantom diffs.** Git for
  Windows' `core.autocrlf=true` default checks files out with CRLF, which the
  Linux container reads as every file modified. `booley init` handles it: it
  inspects both the RTL repository and the Booley Directory's repository
  when they are separate. In each repository it sets
  `core.autocrlf=false` locally. With Stealth enabled, the Project Root's
  `* text=auto eol=lf` default goes in Git's common `info/attributes`, local to
  the repository and shared by linked worktrees; it does not travel through
  commits. Existing upstream attributes policy suppresses that default to
  preserve its rules. If a sibling worktree's user attributes or Git's system
  attributes path cannot be inspected, init also defers a new shared default
  and preserves existing local rules and registrations. Doctor reports this
  limitation as `git.attributes-policy-unknown`, independently of real policy
  conflicts. Inspect the policy from an environment where its source is
  accessible; see [Doctor health](https://booley.dev/docs/doctor/) for source-specific guidance and
  waiver behavior. Normal init remains nonfatal, while unknown-only
  `booley init --check` exits 1 for incomplete inspection.
  With Stealth disabled, and in a Versioned Booley Directory, init adds the default as the first line of root `.gitattributes`
  unless the root already owns a whole-tree policy. `text=auto` preserves Git's
  binary-file detection, and later rules in that file can override the default.
  **Commit only published root `.gitattributes` files** to share their policy.
  On Stealth opt-out, init migrates its replayable local default to root
  `.gitattributes`, unless the root already owns a whole-tree policy, and
  preserves compatible comments and export attributes in the local file.
  Init still warns about a prior untracked root default, a local default
  conflicting with upstream rules while Stealth is enabled, or non-replayable
  local content on opt-out. Inspect those files and remove or migrate the
  default if appropriate; init preserves them because ownership cannot be proven.

    Files already on disk with CRLF are a separate matter. From a clean tracked
    tree, init stages Git-filtered LF replacements, verifies that the affected
    files have not changed since inspection and that replacements change only
    CRLF to LF, then atomically replaces them and reconciles Git's cached metadata
    for only those paths. Content edits made while init runs are preserved and
    reported as unsafe. This preserves
    filesystem metadata, leaves the index content unchanged, and leaves untracked
    and unaffected tracked files alone. Init refuses dirty trees, Git-protected
    affected paths, and hard-linked candidates. The one exception is Booley's own
    Windows guidance fallback: when the root `AGENTS.md`/`CLAUDE.md` are untracked
    hardlinks to the resolved Booley Directory's `AGENTS.md`, init releases them,
    normalizes the canonical file, and recreates the links. Failed tracking
    inspections preserve the links; a partial release is reconciled even if
    another link is locked. Any other hardlink to a candidate
    still blocks the repair. If reconciliation remains unsafe,
    normal init reports setup incomplete and names the affected repository and
    refusal; `--check-only` reports pending work without changing files. Commit
    or stash changes and rerun:

    ```bash
    booley init
    ```

    `booley doctor` re-asks both repositories every run and identifies which one
    is unsafe, so a config reset, a fresh clone that drifts back to CRLF, or stale
    index metadata left by an earlier repair gets caught rather than surfacing as
    phantom diffs in the container. `--fix-line-endings` is accepted; clean
    CRLF checkouts receive the same automatic repair with or without it.

    (Doing this by hand is fiddlier than it looks: `git checkout -- .` on its own
    is **not** enough. With the clean filter in place the worktree files already
    match the index, so Git decides nothing needs rewriting and leaves the CRLF on
    disk. Init materializes filtered replacements separately and applies them only
    after all safety checks pass; it never deletes the originals.)

- **Init cannot resolve the trusted host Booley executable.** A per-user Python
  install normally places `booley.exe` under the user scripts directory rather
  than the interpreter's system `Scripts` directory. Current Booley releases
  inspect both trusted locations even when the user scripts directory is not on
  `PATH`. If the error remains, restore the canonical installed Booley launcher
  (check `pipx list` or `uv tool list` for isolated installs), or add its scripts
  directory to `PATH`, open a new
  terminal, and rerun init.

- **The first sandbox image build takes over an hour.** First builds compile
  EDA tools from source and can take well over an hour on a WSL2-backed Docker;
  the build timeout is overridable via `BOOLEY_IMAGE_BUILD_TIMEOUT` (seconds,
  default 7200).

- **An image build is refused for insufficient disk capacity.** Booley checks
  Docker's reported storage filesystem before the complete known build sequence
  and again before each remaining build. The error names the sequence and shows
  available and required space, each image estimate, one safety reserve, and
  current/reclaimable build-cache usage. `docker builder prune` can free unused
  cache, but it can also evict layers the planned retry needs to rebuild; inspect
  the complete-sequence estimate before choosing that tradeoff. This is related
  to the recovery behavior discussed in [issue #790](https://github.com/boldaxolotl/booley/issues/790).
  Booley never prunes cache or removes images, volumes, Project artifacts, or
  user data automatically. To explicitly accept disk-exhaustion risk, or when
  Docker's reported root is not the filesystem that actually stores its data,
  bypass only that invocation with
  `BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1`; any other value keeps the check enabled.

- **Docker reports `No space left on device` after the capacity preflight
  passed.** An image recipe can grow beyond the conservative estimate. Booley
  retains Docker's error and warns that pruning may discard reusable layers.
  Recheck the complete sequence after cleanup. A completed preflight is not
  permission to delete cache, images, volumes, or the contents of the Booley Directory.
