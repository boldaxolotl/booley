# Give each concurrent Interactive session its own worktree

Status: proposed (2026-11-03)

## Context

All Developer Agent sessions of a Project run in one Sandbox and, by default,
in one checkout (`/work`). Two sessions that edit files overwrite each other.
Today the human has to notice this and ask the agent to run
`booley worktree new <name>`
([USAGE.md](../user/USAGE.md#several-agents-at-once)). The rest of the
lifecycle is manual too:

- Booley has no command to list or remove worktrees. Removal is a seven-step
  procedure ([CLI-REFERENCE.md](../user/CLI-REFERENCE.md)): save the detached
  HEAD, copy out evidence, clean, remove a Paired Checkout before its outer
  worktree, force-remove when submodules are populated, delete branches.
- The shared MCP server runs with cwd `/work`. A call that omits `work_dir`
  runs against the main checkout (`mcp/call_context.py:83-90`), so an agent in
  a worktree must pass `work_dir` on every call or silently test the wrong RTL.
- The post-setup hook does not run in a new worktree.
- A worktree starts on a detached HEAD, so its commits are lost when it is
  removed unless someone made a branch first.

Other agentic development environments have converged on the tool owning the
worktree. Orca creates one per workspace, runs a setup script, and archives or
deletes it. Codex starts each one on a detached HEAD and caps how many it
keeps. Claude Code creates `.claude/worktrees/<name>` and asks keep or remove
on exit. Warp mostly leaves worktrees to the user. Their common failure is
cleanup that deletes live work (Orca issue 23479).

Booley differs from them in two ways. The Sandbox, not the worktree, is the
isolation boundary, so a worktree only separates edits. And `booley chat`
already launches the agent CLI itself (`harness/chat_cmd.py`) and reserves the
session's handle (`S<n>`, ADR 0068) under the agent process's identity before
it starts, so Booley can choose the checkout before the agent starts.

Goal Mode keeps its own entry rules (ADR 0067). This ADR is about Interactive
Mode.

## Decision

### When `booley chat` creates a worktree

A new `[chat] worktree` knob in `booley.toml` takes `auto` (default),
`always`, or `never`. `booley chat --new-worktree` and `booley chat --here`
override it for one launch. The names avoid Claude Code's own `--worktree`
flag, which still passes through to the CLI unchanged.

- **auto.** The session runs in the main checkout unless the main checkout is
  in use, in which case Booley creates a worktree for it. The main checkout is
  in use when either:
  - another `booley chat` holds the main-checkout lock, an exclusive lock that
    `booley chat` takes on a file in Common Booley State
    and holds for the session's lifetime; or
  - the Session Registry has a live Session whose checkout is `/work`. This
    catches a plain `claude` tab or the VS Code agent panel, which take no
    lock. A Session appears there only after its first Booley tool call, so
    this check is best effort.

  A single-agent user sees no change.
- **always.** Every session gets a new worktree. It still takes the lock when
  it is free, so a later `auto` session knows the main checkout is in use.
- **never** and **--here.** The session runs in the main checkout. It takes
  the lock when free and does not wait for it.

### Routing a session's calls to its checkout

A Booley tool call that omits `work_dir` targets the checkout the calling
agent process is running in, not the server's cwd. The server already
resolves the calling process (the peer) to identify its Session; it reads that
process's cwd and uses it when it is the main checkout or a linked worktree of
the same repository. Otherwise the call falls back to the server's cwd, as
today. An explicit `work_dir` still wins.

This is what makes a worktree session work without the agent knowing it is in
one. It also covers sessions started in a worktree without `booley chat`. It
changes the ADR 0068 rule that Session identity is presentational only: the
peer's cwd now selects the checkout, though the Session still never selects a
Goal Record.

Implemented by #2606 (2026-11-08): only a linked worktree of the server's
repository other than the server cwd routes. The main checkout is the server
cwd, so a main-checkout caller keeps today's behavior, including the Goal
refusal. The session process is the Claude Code peer or the marked
`booley chat` Codex owner; ordinary and hand-started Codex never route. See
ADR 0068 Amendments (2026-11-08).

### Launching the agent

`booley chat` forks. The child reserves the session handle under its own
process identity, marks the Codex allocation as ADR 0068 requires, changes to
the selected checkout, and execs the agent CLI. Exec keeps the child's PID and
start time, so the handle, the Session Registry key, and the hook key all name
the agent process, exactly as today. The parent stays alive to hold the lock
and run the exit step.

Terminal handling follows the pattern `sudo` uses for interactive children:
the child runs in its own process group as the terminal's foreground group;
the parent ignores the interactive signals (SIGINT, SIGQUIT, SIGTSTP,
SIGTTOU), forwards SIGTERM and SIGHUP, waits with `WUNTRACED`, stops itself
when the child stops, and takes the terminal back only after the child exits.
`booley chat` returns the child's exit status.

### What the worktree looks like

- **Name.** The worktree is named after the session's handle: Session `S3`
  works in `.booley/worktrees/s3`. Two rules keep the names matched:
  - A handle stays reserved while a worktree of that name exists, which adds
    one rule to the session-entry rules of the ADR 0068 2026-10-29
    handle-pruning amendment.
  - Allocation skips a number whose worktree, `booley/s<N>` branch, or
    `booley-worktree/s<N>` branch already exists. Without this, numbering
    that restarts at 1 after a full prune would collide with branches that
    `rm` kept.

  Implemented by #2610 (2026-11-10): see ADR 0068 Amendments (2026-11-10).
- **Branch.** The worktree is created on a new branch `booley/<name>` from the
  main checkout's HEAD, not on a detached HEAD. Commits therefore stay
  reachable after the worktree is removed. `booley worktree new` switches to
  the same default.
- **Clean start.** The worktree starts from HEAD. Uncommitted changes in the
  main checkout are not copied.
- **Worktree Booley Directory.** Paired Checkout and Snapshot both stay. When
  a Versioned Booley Directory has uncommitted or untracked files, the Paired
  Checkout is made from its HEAD anyway (today this is refused) and Booley
  prints a warning naming the files that were left out. This matches the
  clean start of the RTL checkout. A repository with no commits is still
  refused.
- **Setup.** Submodule fill and the project's post-setup hook run in the new
  worktree before the agent starts, as for baseline checkouts (#2565). Each
  step prints a progress line. If a step fails, Booley rolls the new worktree
  back, which frees its handle, prints the log path, and exits without
  starting the agent.
- **Banner.** `booley chat` prints the worktree path before the agent starts.
  v1 does not open a VS Code window on it.
- **Trust.** The worktree is under the Project Root, which ADR 0079 trusts,
  and Claude trusts everything below a trusted directory. Whether Codex does
  the same is unverified. If it does not, Booley writes a Codex trust entry
  when it creates the worktree and removes it on `rm` (Orca issue 24697 is the
  leftover-trust failure to avoid).

  Implemented by #2612 (2026-11-11): with Codex 0.160.0 (the pinned Sandbox
  version), a worktree under the trusted Project Root gets no trust prompt, so
  no trust entry is written or removed. The main-checkout lock is
  `runtime/chat-main.lock`, held by the parent `booley chat` until the child
  exits (a crash releases it). `auto` releases the lock when only a registry
  Session made it pick a worktree. A launch from a linked worktree runs in
  place. The worktree is named `s<N>` from the session handle, and a launch
  that cannot reserve a handle refuses with exit 2.

### Resume

Booley records, per worktree, the agent conversation ids started in it. The
session-start hook supplies them (Claude `session_id`, Codex thread id) along
with the cwd. `booley chat --resume <id>`, Claude `-c`/`--continue`, and Codex
`resume <id>`/`resume --last` open in the worktree the conversation belongs to,
when it still exists, and alias the new process key to the old handle through
the existing `predecessors` mechanism, so S3 resumes as S3. A conversation
whose worktree is gone resumes in the main checkout, with a notice.

Implemented by #2610 (2026-11-10): the records only. The hook writes
`worktrees/.conversations/<hash>.json`; nothing resumes from it yet. See ADR
0068 Amendments (2026-11-10).

### Exit

When the agent exits, the parent asks: keep or discard.

- Booley never merges. The prompt lists what discard would lose, so the human
  can merge it first:
  - uncommitted changes, in the worktree and in its Paired Checkout;
  - commits on the worktree's current branch, which may be a Goal Branch
    after Goal Mode, and on its `booley-worktree/<name>` branch, that are not
    in the main checkout's branch. Commits that were squash-merged count as
    merged when they are patch-equivalent (`git cherry`).
- Discard runs `booley worktree rm`. When `rm` refuses, the prompt asks a
  second time before forcing.
- No prompt means keep: a non-interactive exit, and a closed terminal (the
  parent gets SIGHUP). Closing the terminal is the commonest exit, so kept
  worktrees are normal; `gc` and `list` handle them.

### Lifecycle commands

`booley worktree` gains `list`, `rm <name>`, and `gc`.

- `list` shows each worktree's branch, Booley Directory kind, uncommitted and
  unmerged work, live Session, active Goal Record, and last-used time.
  Last-used is a stamp in Common Booley State, updated on every launch and Booley
  tool call from that worktree; Session Registry rows expire too soon to serve.
- `rm` performs the removal procedure that is manual today, in order:
  evidence (including `handoffs/`, `reviewer-evidence/`, Flow reports and
  traces) moves to Common Booley State, a Paired Checkout is removed before its
  outer worktree, populated submodules are handled, then the worktree is
  removed. Branches are kept.
- `rm` refuses, unless forced, on a live Session, an active Goal Record,
  uncommitted changes in either repository, or unmerged commits as defined
  under Exit. This is the check Orca lacks.
- `gc` runs `rm` without force over worktrees whose last-used stamp is older
  than an age (default 7 days). `booley chat` runs it at launch and prints only
  what it removed.

Existing worktrees need no migration. Detached ones are listed as detached;
`rm` and `gc` treat their HEAD commits as unmerged until they are on a branch.

### Common and Checkout Booley State

The Booley Directory is split explicitly into two parts, and every path in it
belongs to exactly one. The names follow git, whose common dir is shared by
every worktree while each worktree keeps its own per-checkout files.

- **Common Booley State**: one per Project, in the Main Booley Directory,
  used by every checkout. It holds state that coordinates sessions and Jobs
  or that is a cache safe to share.
- **Checkout Booley State**: one per checkout, in that checkout's Booley
  Directory. It holds configuration versioned with the code and results tied
  to one checkout. The Main Booley Directory holds the main checkout's
  Checkout Booley State next to the Common Booley State; a Worktree Booley
  Directory holds only Checkout Booley State.

One rule decides which directory a caller gets:

- `BOOLEY_DIR`, `[project].dir`, or walk-up selects the Project. It may name
  any Booley Directory of that Project.
- The common resolver maps that selection to the Main Booley Directory through
  the git common dir, from any checkout.
- The checkout resolver returns the Booley Directory of the checkout being
  worked on (`resolve_checkout_booley_dir`).

So commands Booley runs for a checkout (Pre-Sim Commands, the post-setup
hook) get one directory in `BOOLEY_DIR`, the checkout's own, and a Booley
command they start still reaches Common Booley State in main. The path-by-path
classification is in
[Appendix: Booley Directory split](#appendix-booley-directory-split).

## Rejected alternatives

**Always create a worktree.** Most users run one agent, and for them a
worktree costs setup time (submodule fill, post-setup hook, cold Verilator
build) and splits what the editor shows (`/work`) from what the agent edits.
`always` remains available as a knob.

**Keep `execvp` and inherit the lock across exec.** This keeps the process
identity without a fork, but there is no process left to run the exit step,
and every long-lived process the agent spawns inherits the lock and holds it
after the agent exits.

**Detect "main in use" from the Session Registry alone.** A Session is
registered only at its first Booley tool call, so two `booley chat` launches
moments apart would both pick the main checkout. The lock closes that race;
the registry check covers clients that take no lock.

**Have `booley chat` pin `work_dir` in a per-session MCP configuration.** It
covers only sessions `booley chat` launched and needs a different mechanism
for Claude and Codex. Reading the peer's cwd covers every client.

**Use Claude Code's native `--worktree`.** It knows nothing of Paired
Checkouts, submodule fill, or the post-setup hook, puts worktrees outside
Booley's registry, and Codex has no equivalent.

**Copy uncommitted changes into the new worktree.** It surprises the session
that owns those changes and duplicates them in two checkouts.

**Merge or cherry-pick on exit.** Picking the target branch, conflicts, and
partial work are decisions for the human. Booley warns and leaves them there.

**Fall back to a Snapshot when the Versioned Booley Directory is dirty.**
Edits to a Snapshot cannot be merged back, so configuration changes made in
that worktree would be silently stranded.

**Suffix the name on a handle collision (`s2-2`).** The Session handle and the
path would stop matching, which is the point of naming them alike.

**Tag the terminal with the worktree name.** The agent CLI's terminal already
shows the session handle, which now equals the worktree name.

## Consequences

- Parallel sessions stop overwriting each other without the human having to
  remember to ask for a worktree, and their tool calls reach their own RTL
  without `work_dir`.
- The second and later sessions wait for worktree setup before the agent
  starts. On a large Project (many submodules, a slow post-setup hook) that
  can take minutes, and each kept worktree uses disk until `gc`.
- `booley chat` becomes a parent process that owns terminal handoff and
  signals for the session's lifetime. Bugs there show up as a stuck terminal,
  so this part needs manual testing with both CLIs.
- The session handle and the worktree are tied: removing the worktree is what
  frees the handle.
- The peer's cwd now selects a call's checkout. ADR 0068 needs an amendment
  for this and for handle retention and allocation.
- Code that resolves the Booley Directory must say which part it wants. Doctor
  can then check that no Common Booley State path exists inside a Worktree Booley
  Directory.

## Appendix: Booley Directory split

### Classification

Checkout Booley State is versioned with the RTL, or tied to one checkout's
sources. Everything else is Common Booley State.

| Part | Paths (relative to the Booley Directory) | Why |
|---|---|---|
| Checkout | `booley.toml`, `tests.toml`, `criteria.toml`, `doctor-waivers.toml`, `configs.toml`, `cores/`, `hooks/`, `generators/`, `mcp_tools/`, `selftest/`, `AGENTS.md`, `FUSESOC_IGNORE`, `goals/history/`, `tickets/history/` | Versioned configuration; it changes with the code on the worktree's branch |
| Checkout | `.runtime/edalize/` and its `.locks/`, `.runtime/python-artifacts/`, `flow-reports/`, `mcp-tool-reports/`, `tmp/setup/` | Build trees and Flow evidence for one checkout's sources; concurrent writers must not collide |
| Common | `goals/<id>/`, `runtime/jobs/slots/`, `runtime/job-handles/`, `runtime/sessions/` (registry and handle map), `runtime/session_activity/`, `runtime/dashboard-task.lock`, `.interactive_logs/`, the main-checkout lock, worktree last-used stamps and conversation records | Coordinates sessions and Jobs across the whole Sandbox |
| Common | `runtime/doctor_stamp.json`, `runtime/doctor_deep_stamp.json`, Doctor probes, `runtime/upgrade_review.json` | Describes the Sandbox and installed package, not the code |
| Common | `.runtime/compiler-cache/`, `result-cache/` (ADR 0076) | Content-addressed or keyed by committed state; safe and useful to share |
| Common | `worktrees/` and `worktrees/.locks`, `.baseline-wt-*`, `directory-layout.lock`, `.directory-relocation.json` | Registry and scratch for every checkout |
| Common | `findings.jsonl`, `BOOLEY-FEEDBACK.md`, `setup-evidence/`, `reviewer-evidence/`, `reviewer-state/`, `handoffs/`, `tickets/` runtime state | Project-wide logs and evidence; Reviewer state is already keyed by checkout |
| Common, read from main | `goalsets/` | The glossary makes the Main Booley Directory the owner of Goalsets, and Goal entry reads them from main (`goals/entry.py:352,536`) |
| Common, built from main | `docker/` | Versioned, but the Project has one Sandbox image; a worktree's copy cannot change the running image |

### Resolution

- Common Booley State resolves through one function that returns the Main Booley
  Directory, by way of the git common dir, from the Project that `BOOLEY_DIR`,
  `[project].dir`, or walk-up selected. `goals/input_identity.py`
  `main_booley_dir_entry` does the git-common-dir step today but ignores the
  environment; the new resolver must not.
- Checkout Booley State resolves through `resolve_checkout_booley_dir`.
- Walk-up resolution of runtime state, `Path.cwd() / ".booley"`, and literal
  `<root>/.booley` paths are retired.
- Snapshot creation copies only Checkout Booley State (an allowlist), replacing
  today's exclusion list.

### Defects this fixes

Found while classifying, against `main` at e94d30904. Each is tagged with
where it occurs: **host** (no `BOOLEY_DIR`, so resolution walks up from the
cwd) or **Sandbox** (`BOOLEY_DIR=/booley-project`, the main directory).
Interactive worktrees make a worktree cwd the normal case, so each of these
becomes common.

1. **Host.** Host commands (`doctor`, `upgrade`, `feedback`, `cleanup`,
   `targets`, `resources`) run from a worktree resolve that worktree's own
   Booley Directory by walking up (`core/booley_dir.py:165-175`). Slots,
   findings and handles then go to the worktree's copy.
2. **Both.** Doctor lists worktrees under the checkout's Booley Directory
   instead of the main one (`harness/setup/readiness.py:323`,
   `harness/doctor.py:1680`). Run from a worktree, it misses the real
   registry and the core-shadow guard.
3. **Both.** Doctor writes its stamp per checkout (`harness/doctor.py:850`)
   but the staleness notice reads the main one
   (`runtime/doctor_stamp.py:266`).
4. **Both.** `booley upgrade ack` from a worktree writes per checkout
   (`harness/upgrade_cli.py:85`). The registrar and Dashboard read main and
   keep reporting a pending review.
5. **Both, latent.** The MCP server derives its interactive log and
   Job-record directory from its own cwd (`mcp/server.py:2151`). The shared
   Sandbox server always runs in `/work`, so this bites only an MCP server
   started from a worktree; its Jobs are then invisible to the Dashboard and
   handle pruning, which scan only main.
6. **Sandbox.** A `--baseline` run for a worktree copies main's `cores/` and
   `booley.toml` instead of the worktree's
   (`flows/baseline_worktree.py:369`).
7. **Sandbox.** Pre-Sim Commands get `BOOLEY_DIR` set to main
   (`flows/sim/execution/pre_sim.py:115`) while the Flow reads the worktree's
   configuration.
8. **Both.** The post-setup hook gets `BOOLEY_DIR` set to the checkout's
   directory (`runtime/project_prepare.py:77`), and today's resolvers then
   treat that copy as the whole Project, so Booley commands run by the hook
   resolve Common Booley State per worktree. The resolution rule above fixes this
   without changing what the hook receives.
9. **Sandbox.** A single Reviewer run writes state per checkout
   (`specialists/reviewer.py:944`) and evidence to main (`:1693`).
10. **Both.** The Snapshot exclusion list misses
    `runtime/session_activity/`, the Doctor stamps and probes,
    `runtime/upgrade_review.json`, `runtime/dashboard-task.lock`,
    `mcp-tool-reports/`, `reviewer-state/`, `reviewer-evidence/`,
    `findings.jsonl` and `setup-evidence/`. Its `.locks` and `tmp` patterns
    are unanchored and also drop nested paths with those names
    (`runtime/worktree_create.sh:498-499`).
11. **Both.** Literal `<root>/.booley` paths ignore a relocated Booley
    Directory (`runtime/worktree_create.sh:104,150`,
    `harness/worktree_cmd.py:96`, `core/build_paths.py:12`,
    `fusesoc/core_projection.py:75`).
