# Developer Agent child rules

The operator applies these rules in every mission area that hands design work
to a Developer Agent child: a Developer Agent the operator starts in the
Sandbox and asks for a Measured Commit. Each mission links this file from its
rules and adds its own details per area.

- Before the request, commit the area's Target Metrics: merge the named
  `metrics.toml` fixture into the Booley Directory's `metrics.toml` (in
  Stealth Mode, commit it in the Booley Directory's own repository with
  `git -C .booley commit`), then run `booley measure --validate`: no error.
- Start a Developer Agent (Codex or Claude) in a T3 Code thread in a new
  worktree for the area, and note the worktree's path and the branch T3 Code
  names. Send the area's prompt verbatim; it ends with "give me a Measured
  Commit". The operator never does the child's design work.
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
  `booley-worktree/<area>`) into its run-owned destination, record in
  `resources.md` only the branches actually created, then remove the worktree
  by deleting its T3 Code thread; commits stay on the branches. Leave every
  participating destination clean before the next worktree.
