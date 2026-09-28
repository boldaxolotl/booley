# Gate portable Ticket Workspace links on both Git clients

Booley configures its outer Project repository and standalone Project-data
repository to create relative worktree links only when both the host and the
selected Sandbox Image provide stable Git 2.48 or newer. Before that opt-in, an
old or unverified side retains the absolute, Sandbox-only fallback. Worktree-local
`core.worktree` and `core.hooksPath` values are relative too, so relocating the
same mounted checkout does not break Git or bypass Ticket Scope hooks.

---
status: accepted
---

## Considered Options

- Passing `--relative-paths` at every worktree creation call was rejected because
  older Git rejects it and new creation or recovery paths could omit it.
- Enabling relative links from the host version alone was rejected because the
  Sandbox opens the same repositories and must understand their format.
- Rewriting every live Ticket Workspace during Project Initialization or Doctor
  was rejected because interruption could leave one side of Git's two-way
  worktree registration inconsistent.
- Removing the prune guard after opt-in was rejected because legacy worktrees,
  pre-opt-in fallback, and temporary Sandbox-only worktrees still need it.

## Consequences

`worktree.useRelativePaths` is a Booley-managed repository policy. Creating a
relative worktree upgrades the repository to format 1 with
`extensions.relativeWorktrees=true`; after that transition, Git older than 2.48
is an incompatible downgrade rather than a fallback. Booley diagnoses that state
and requires restoring a supported Git before any deliberate downgrade migration.
Such a migration must repair and validate every registration and relative
worktree-local path before removing the extension, so it is not automated here.

Existing worktrees age out normally. Explicit move and Project relocation repair
operations may normalize their metadata under their existing atomic contracts.
Automatic pruning remains disabled in both repositories.
