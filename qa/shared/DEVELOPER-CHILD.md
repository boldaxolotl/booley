# Developer Agent child rules

The operator applies these rules in every mission area that hands design work
to a Developer Agent child: a Developer Agent the operator starts in the
Sandbox and asks for a Measured Commit. Each mission links this file from its
rules and adds its own details per area.

A Developer Agent child, Interactive or measuring, runs in the Sandbox in
one of two ways. Users have only the first; QA may use either and records
which in `log.md`:

- **T3 Code thread.** After `booley session up` in the mission's Project,
  pair a browser tab of your own with its T3 Code as step 4 of the
  [T3 Code GUI QA runbook](t3/RUNBOOK.md) says, keeping the pairing link
  secret as its Hard rules say, and start a thread in the child's worktree.
- **Agent CLI in the Sandbox.** From the checkout's directory on the host,
  run `booley session enter -- codex` (or `claude`); a linked worktree's
  directory starts it in that worktree.

Never start a child as a `claude` or `codex` process on the host itself: it
would run outside the Sandbox. To stop a child, stop its turn or end its
session; a restarted child is a new thread or session.

- Before the request, commit the area's Target Metrics: merge the named
  `metrics.toml` fixture into the Booley Directory's `metrics.toml` (in
  Stealth Mode, commit it in the Booley Directory's own repository with
  `git -C .booley commit`), then run `booley measure --validate`: no error.
- Give the child a new worktree for the area, and note its path and its
  branch. A T3 Code thread started in a new worktree gets one that T3 Code
  creates, names and prepares. For the agent CLI way, make it in the Sandbox
  with `git worktree add` and run `booley worktree prepare` in it
  ([Worktrees](../../docs/user/WORKTREES.md)). Start the child (Codex or
  Claude) there in one of the two ways above, and send the area's prompt
  verbatim; it ends with "give me a Measured Commit". The operator never does
  the child's design work.
- The child follows the measuring rules (`booley cheat --metrics`). The
  operator is the deciding human for disposable QA work: a request to add a
  table or change `metrics.toml` or `tests.toml` gets an approval or a
  rejection with a reason in the chat; a change to a merged deliverable needs
  the live maintainer; silence never approves.
- Check the result yourself. In the child's worktree,
  `booley measure --base <commit the worktree started from> <reported sha>`
  prints the stored Measurement Run against that Base, starts no Flow and
  exits 0. The header is green; "rules loosened" or "tests changed" without an
  approval is a finding. Name the Base explicitly: the default Base is the
  merge-base with `origin/HEAD` or local `main`, which on a run-owned branch
  of an upstream clone predates the QA rules, and a commit on the default
  branch's own history has none. Save the Markdown and JSON Measurement Report
  paths.
- From the host, run container commands as
  `booley session enter -- booley ...`; a Sandbox command written without that
  prefix is meant for the child's terminal inside the container.
- Integration: merge the worktree's branch (in Stealth Mode also
  `booley-worktree/<folder>`) into its run-owned destination, record in
  `resources.md` only the branches actually created, then remove the
  worktree: delete its T3 Code thread, or run `git worktree remove` for one
  made with Git. Commits stay on the branches. Leave every participating
  destination clean before the next worktree.
