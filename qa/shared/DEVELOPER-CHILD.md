# Developer Agent child rules

The operator applies these rules in every mission area that hands design work
to a Developer Agent child: a Developer Agent the operator starts in the
Sandbox and asks for a Measured Commit. Each mission links this file from its
rules and adds its own details per area.

Every Developer Agent child, Interactive or measuring, is a T3 Code thread.
After `booley session up` in the mission's Project, pair a browser tab of
your own with its T3 Code as step 4 of the
[T3 Code GUI QA runbook](t3/RUNBOOK.md) says, under that runbook's Hard
rules, and drive the child from that tab. A bare `claude` or `codex`
session, on the host or in the Sandbox, is not a supported workflow: never
start one as a child, and never count its results. To stop a child, stop
its thread's turn; a restarted child is a new thread.

- Before the request, commit the area's Target Metrics: merge the named
  `metrics.toml` fixture into the Booley Directory's `metrics.toml` (in
  Stealth Mode, commit it in the Booley Directory's own repository with
  `git -C .booley commit`), then run `booley measure --validate`: no error.
- Create the area's worktree from the host with
  `booley session enter -- booley worktree new <area>`, and note its path and
  its branch (`booley/<area>`; `wt/<area>` in Stealth Mode). Start a Developer
  Agent (Codex or Claude) in a T3 Code thread on that worktree, and send the
  area's prompt verbatim; it ends with "give me a Measured Commit". The
  operator never does the child's design work.
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
- Integration: merge the worktree's branch (`booley/<area>`; in Stealth Mode
  `wt/<area>` and `booley-worktree/<area>`) into its run-owned destination,
  record in `resources.md` only the branches actually created, then remove the
  worktree with `booley session enter -- booley worktree rm <area>`, which keeps
  evidence and branches. Leave every participating destination clean before
  the next worktree.
