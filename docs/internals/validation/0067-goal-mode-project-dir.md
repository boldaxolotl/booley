# Goal Mode Project directory spike (ADR 0067, Phase 0, fact 19)

Validated on 06 OCT 2026 for [ADR 0067](../../adr/0067-replace-ticket-mode-with-goal-mode.md).
Decides where the committed Goal summary is written so it lands on the Goal
Branch.

## Setup

Run inside the picorv32 Sandbox (Booley 0.3.0 at `07aef4f3f`,
`BOOLEY_CONTAINER=1`, `BOOLEY_PROJECT_DIR` set by the devcontainer) as the
`agent` user. For each variant the script created a branch, created a
linked worktree with Booley's own `worktree_create.sh` on that branch, then
recorded the resolution APIs, wrote
`<worktree>/.booley_project/goals/history/x.md`, and committed it with
`runtime.history_commit.commit_file` (the Step 0 R7 publisher: private
index, compare-and-swap on the branch).

Variants:

- `stealth`: the real checkout at `/work` (Stealth: `.booley_project` is
  its own repository; `/.booley_project` in `.git/info/exclude`).
- `excl`: a clone where `.booley_project` is tracked (non-Stealth), with
  the `init` exclude line present.
- `noexcl`: the same, exclude line absent.

## Results

| | stealth | excl | noexcl |
| --- | --- | --- | --- |
| Worktree has its own `.booley_project/` | yes (copied by `worktree_create.sh`, no nested `.git`) | yes | yes |
| `resolve_project_dir(start=worktree)` with `BOOLEY_PROJECT_DIR` set | the shared Project directory (env wins) | shared | shared |
| `resolve_project_dir(start=worktree)` without the env | the worktree's copy | worktree's copy | worktree's copy |
| `resolve_checkout_project_dir(worktree)` | worktree's copy | worktree's copy | worktree's copy |
| `discover_project_root(<worktree>/.booley_project/goals/history)` | the worktree | the worktree | the worktree |
| `git rev-parse --show-toplevel` from `goals/history` | the worktree | the worktree | the worktree |
| `x.md` ignored? | yes (`info/exclude`) | yes (`info/exclude`) | no |
| `commit_file` on the worktree branch | committed | committed | committed |
| `git ls-tree HEAD` shows the file | yes | yes | yes |
| Main checkout afterwards | clean | clean | `?? .booley_project/worktrees/` |

Ignore rules do not matter: `commit_file` stages with
`update-index --cacheinfo` and builds the commit from a private index, so
an excluded path commits like any other. The main checkout's HEAD and
index were untouched in every variant.

Two more facts the spike surfaced:

- `worktree_create.sh` with an empty `branch_ref` creates a detached
  worktree, and `commit_file` refuses a detached HEAD. The Goal Branch must
  exist and be checked out at publication; D8 (branch created in place at
  entry) creates it, and the `check_branch` binding above enforces it at
  Finish.
- Inside the Sandbox, `BOOLEY_PROJECT_DIR` points every `resolve_project_dir`
  call at the shared Project directory even with an explicit `start`, so a
  Goal record keyed by that resolution lives in the shared directory, not
  in the worktree's copy. That is the intended place for the local record.

## Decision for D9

- The local Goal record stays in the Project directory that discovery
  returns (`.booley_project/goals/<goal-id>/`, shared in a Sandbox).
- Outside Stealth, the committed summary is a separate file written at
  `<goal worktree>/.booley_project/goals/history/<goal-id>.md` and committed
  from the worktree with the repository taken from the worktree
  (`git rev-parse --show-toplevel` from the history directory), never from
  the Project directory. The commit binds the branch: `commit_file` is
  called with a `check_branch` callback that raises unless the full ref it
  receives is the Goal Branch, and `commit_file` itself refuses a detached
  HEAD, since the publisher otherwise commits onto whatever HEAD is
  current. Both
  non-Stealth layouts were proven.
- Under Stealth the record stays local and nothing is committed, as ADR
  0067 already says; the `stealth` variant only shows that the mechanics
  would work if that rule ever changed.
- `init` keeps the `/.booley_project` exclude line as is; without it a
  non-Stealth checkout shows every linked worktree as untracked.
  `project_gitignore.py` ignores `goals/*/` and keeps `goals/history/`.
