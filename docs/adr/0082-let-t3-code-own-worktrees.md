# Let T3 Code own worktrees

Status: accepted

## Context

All Developer Agent sessions of a Project run in one Sandbox. Two sessions
that edit one checkout overwrite each other, so each needs its own worktree.
T3 Code, Booley's user interface (ADR 0093), already gives a thread a
worktree: it creates it, names its branch, and deletes it with the last
thread that uses it. Several threads can share one. A second owner beside T3
would disagree with it about names, location and removal, so Booley does not
manage worktrees itself.

T3 does not give a worktree its own Booley configuration, run the Project's
post-setup hook, or tell Booley which worktree a Codex call comes from. Every
Codex thread runs in one shared `codex app-server` process whose cwd is T3's
own, so the calling process's cwd, which places a Claude call, says nothing
about a Codex call.

## Decision

- **T3 owns the lifecycle.** Booley creates and removes no worktree, adopts
  any linked worktree of the Project's repository, and keeps T3's branch
  names. Booley's managed T3 policy locks T3's worktree directory to
  `t3-worktrees/` in the Main Booley Directory. A thread in the main checkout
  takes no lock.
- **Booley prepares each new worktree.** The same policy locks a T3 project
  script that runs `booley worktree prepare` when T3 creates a worktree,
  before the thread starts. A failure fails the launch. The command does
  nothing outside a Booley Project and refuses when the Booley Directory lies
  outside the checkout, because a worktree must never silently share the main
  checkout's configuration. Otherwise it gives the worktree its own Booley
  Directory, runs the post-setup hook, and prunes registrations left by
  deleted Paired Checkouts. A Booley Directory tracked in the RTL repository
  needs nothing. A Versioned Booley Directory gets a Paired Checkout on a
  branch named after the worktree's folder, because T3 renames the RTL branch
  after creation, possibly while the script runs. Any other gets a Snapshot.
  T3 fills submodules itself.
- **Routing.** A Booley tool call that omits `work_dir` targets the caller's
  checkout when that is a linked worktree of the server's repository, and the
  server's cwd otherwise. An explicit `work_dir` wins. A Project-defined
  endpoint without a `work_dir` parameter is refused for a routed caller
  rather than run on the main checkout.
  - Claude runs one process per thread, so the calling process's cwd names
    the checkout.
  - Codex names the thread's workspace root in each call's turn metadata.
    When the metadata names none, which happens on a thread's first call, the
    call is refused with a retry message instead of silently testing the main
    checkout's RTL.

### Common and Checkout Booley State

The Booley Directory splits in two, and every path belongs to exactly one,
as git's common dir does with per-worktree files. **Common Booley State**
lives in the Main Booley Directory: state that coordinates Jobs, and caches
safe to share. **Checkout Booley State** belongs to one checkout:
configuration versioned with the code, and results tied to its sources. The
classification of each path is in the code. A caller says which part it
wants. The common resolver reaches the Main Booley Directory through the git
common dir from any checkout. The checkout resolver returns the directory of
the checkout being worked on. A Snapshot copies only the versioned part of
Checkout Booley State.

## Rejected alternatives

- **Keep Booley's own worktrees, launcher and lifecycle commands.** Two owners
  disagree, and T3 deletes worktrees without Booley's checks anyway.
- **One Booley Directory for every worktree.** Less code and no lost edits,
  but a configuration change could not be tried in one worktree alone.
- **Prepare lazily on the first routed call.** It needs a per-worktree
  "prepared" marker, and the first call pays for setup.
- **Route Codex by a header helper that prints its cwd.** Under the MCP
  protocol version Booley's server supports, Codex shares one connection
  across threads, so the cached header would name the wrong worktree.
- **Record each thread's cwd in a session-start hook.** It keeps retired
  machinery for a rare case. Add it if refusals turn out to be common.

## Consequences

- Deleting a thread's worktree in T3 force-removes it: uncommitted edits in
  its Paired Checkout are lost without warning, and so are the Flow reports
  and build trees kept in the worktree, because nothing copies them out first.
  Its commits survive on its branch. T3 has no hook or dirty check to stop
  this.
- Engineers who work only in a terminal get no automatic worktree. They make
  one with Git or T3.
- Worktrees that `--baseline` Flow runs make are a Flow's internal detail,
  not sessions' worktrees, and stay.
- Doctor can check that no Common Booley State path exists inside a Worktree
  Booley Directory.
