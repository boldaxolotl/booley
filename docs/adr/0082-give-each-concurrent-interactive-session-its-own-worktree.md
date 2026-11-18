# Give each concurrent Interactive session its own worktree

Status: accepted

## Context

All Developer Agent sessions of a Project run in one Sandbox and, by default,
in one checkout. Two sessions that edit files overwrite each other. Making a
worktree by hand is easy to forget, and the rest of its lifecycle (the shared
MCP server's cwd is the main checkout, setup hooks do not run, a detached HEAD
loses commits on removal, removal needs a long manual procedure) goes wrong
silently. Other agentic environments converged on the tool owning the
worktree, and their common failure is cleanup that deletes live work.

Booley differs in two ways. The Sandbox, not the worktree, is the isolation
boundary, so a worktree only separates edits. And `booley chat` launches the
agent CLI itself and reserves the session handle (ADR 0068) under the agent
process's identity, so Booley can choose the checkout before the agent starts.

This ADR covers Interactive Mode. Goal Mode keeps its own entry rules
(ADR 0067).

## Decision

### Placement

`[worktree] chat` takes `auto` (default), `always` or `never`; `booley chat
--new-worktree` and `--here` override it for one launch. The flag names avoid
Claude Code's own `--worktree`, which still passes through unchanged.

- **auto**: use the main checkout unless it is in use, then create a
  worktree. A single-agent user sees no change. "In use" means another
  `booley chat` holds the main-checkout lock, or the Session Registry has a
  live Session in the main checkout (this catches clients that take no lock,
  best effort because a Session registers only at its first tool call). A
  lock that cannot be opened counts as held.
- **always**: every session gets a worktree, but still takes the lock when
  free so later `auto` sessions see the main checkout in use.
- **never** and `--here`: use the main checkout, take the lock when free,
  never wait for it.

A launch from inside a linked worktree or outside Git runs in place.
Placing a session in a new worktree needs a fork-capable platform.

### Routing a session's calls

A Booley tool call that omits `work_dir` targets the checkout the calling
agent process runs in, not the server's cwd, so an agent in a worktree needs
no knowledge of it. Only a linked worktree of the server's repository
routes; a main-checkout caller keeps the server's cwd. An explicit `work_dir`
wins. This also covers sessions started in a worktree without `booley chat`.
The peer's cwd now selects the checkout, but a Session still never selects a
Goal Record (ADR 0068).

### Launching the agent

`booley chat` forks. The child reserves the handle under its own process
identity, enters the checkout and execs the agent CLI, so the handle, the
registry key and the hook key all name the agent process. The parent stays to
hold the lock and run the exit step, and passes the child's exit status
through. On a terminal the child is the foreground process group and the
parent hands the terminal back only after the child exits, as `sudo` does for
interactive children. Without fork, the agent replaces `booley chat`: no lock,
no exit step.

### The worktree

- **Name and handle.** Named after the session handle: Session `S3` works in
  worktree `s3`. A handle stays reserved while a worktree of that name
  exists, and allocation skips a number whose worktree or branches already
  exist, so numbering never collides with branches kept by `rm`. Name and
  handle never diverge (no `-2` suffix): matching them is the point.
- **Branch.** A new branch from the main checkout's HEAD, never a detached
  HEAD, so commits survive removal. Under Stealth Mode the branch prefix is
  not the banned default term; code that finds or removes a worktree's branch
  reads what Git registered.
- **Clean start.** Uncommitted changes in the main checkout are not copied: it
  would surprise the session that owns them and duplicate them.
- **Booley Directory.** A Versioned Booley Directory gets a Paired Checkout
  from its HEAD even when it has uncommitted files (a warning names what was
  left out); a Snapshot would strand configuration edits that cannot be
  merged back. A Booley Directory outside the checkout refuses worktree
  creation.
- **Setup.** Submodule fill and the project's post-setup hook run before the
  agent starts, as for baseline checkouts. A failed step or a signal rolls the
  worktree back, which frees its handle, and the agent never starts.
- **Trust.** The worktree is under the Project Root, which ADR 0079 trusts;
  Booley writes no extra trust entry for any agent CLI.

### Resume

Booley records, per worktree, the agent conversation ids started in it (from
the session-start hook). A resume form of `booley chat`, Claude `-c`/`--resume`
or Codex `resume`, opens in the worktree its conversation belongs to and
keeps the old handle, so S3 resumes as S3. A conversation whose worktree is
gone resumes in the main checkout with a notice. A worktree with a running
session, and a busy main checkout (except under `never`), are never resumed
into. For "latest" forms the worktree wins only when strictly newer than
main by the agent CLI's own storage.

### Exit

When the agent exits by itself on a terminal, the parent asks keep or
discard, for a worktree this launch created only. No prompt means keep: a
non-interactive exit, a signal, a closed terminal (the commonest exit, so
kept worktrees are normal and `gc` handles them). The exit code stays the
agent's.

Booley never merges: choosing a target branch, conflicts and partial work
are the human's decisions. The prompt lists what discard would lose
(uncommitted changes in the worktree or its Paired Checkout, commits not in
the main checkout's branch, with squash-merged commits counted as merged);
the list is the loss reasons of `rm`, so the two never disagree. Discard runs
`rm`, which asks a second time before forcing.

### Lifecycle commands

`booley worktree` has `list`, `rm <name>` and `gc`.

- `rm` moves evidence to Common Booley State, removes a Paired Checkout
  before its outer worktree, handles populated submodules, then removes the
  worktree. Branches are kept. It refuses, unless forced, on a live Session
  (including a running agent that holds the handle before its first tool
  call), an active Goal Record, uncommitted changes in either repository, or
  unmerged commits. This is the check other environments lack; their
  cleanup deletes live work.
- `gc` is `rm` without force over worktrees idle longer than
  `[worktree] gc_older_than`, judged by a last-used stamp in Common Booley
  State (Session Registry rows expire too soon). `booley chat` runs it at
  launch and prints only what it removed.

### Common and Checkout Booley State

The Booley Directory splits in two, and every path belongs to exactly one.
The names follow git's common dir versus per-worktree files.

- **Common Booley State**: one per Project, in the Main Booley Directory.
  State that coordinates sessions and Jobs, or caches safe to share.
- **Checkout Booley State**: one per checkout. Configuration versioned with
  the code and results tied to one checkout's sources.

The classification of each path is in the code. Callers must say which part
they want: Project selection (`BOOLEY_DIR`, `[project].dir`, walk-up) may name
any Booley Directory of the Project; the common resolver maps it to the Main
Booley Directory through the git common dir from any checkout; the checkout
resolver returns the directory of the checkout being worked on. A command
Booley runs for a checkout gets that checkout's directory in `BOOLEY_DIR`
and still reaches Common Booley State through the common resolver. Snapshot
creation copies only the versioned part of Checkout Booley State (an
allowlist, so an unclassified path is not copied).

## Rejected alternatives

- **Always create a worktree.** Most users run one agent; a worktree costs
  setup time and splits what the editor shows from what the agent edits.
- **Keep `execvp` and inherit the lock.** No process is left for the exit
  step, and every process the agent spawns would hold the lock after it exits.
- **Detect "main in use" from the Session Registry alone.** Two launches
  moments apart would both take main; the lock closes the race.
- **Pin `work_dir` in a per-session MCP configuration.** Covers only
  `booley chat` sessions and differs per agent CLI; reading the peer's cwd
  covers every client.
- **Claude Code's native `--worktree`.** Knows nothing of Paired Checkouts,
  setup, or Booley's registry, and Codex has no equivalent.
- **Merge or cherry-pick on exit; copy uncommitted changes in.** See above.

## Consequences

- Parallel sessions stop overwriting each other, and their tool calls reach
  their own RTL without `work_dir`.
- Second and later sessions wait for worktree setup, which can take minutes
  on a large Project, and kept worktrees use disk until `gc`.
- `booley chat` is a parent process owning terminal handoff and signals for
  the session's lifetime; bugs there show up as a stuck terminal, so changes
  need manual testing with both agent CLIs.
- The handle and the worktree are tied: removing the worktree frees the
  handle.
- Doctor can check that no Common Booley State path exists inside a Worktree
  Booley Directory.
