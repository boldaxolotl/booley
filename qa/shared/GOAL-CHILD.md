# Goal-child operating rules

The operator applies these rules in every mission area that uses a Goal
child: a Developer Agent the operator starts in the Sandbox to do one Goal
Mode's design work. Each mission links this file from its rules and adds its
own Goal details per area.

- The host operator starts a long-lived Codex Goal child in the Sandbox with
  `booley session enter -- codex ...`, then sends `$booley-goal` and the area's
  mission prompt. The skill stops after entry; the operator then clears the
  child's chat with `/clear` and sends `$booley-goal start` to begin the work.
  Claude children use `/booley-goal` and `/booley-goal start`. The operator
  never implements the child's design work. Read
  `src/booley/data/skills/booley-goal/SKILL.md` (including `review.md`) and
  `docs/user/GOAL-MODE.md` from the candidate build for the workflow.
- The operator is the deciding human for disposable QA proposals: answer entry
  choices and approve or reject the exact proposal with a reason, through the
  client form or a message the child quotes as `approval_quote`. A proposal
  affecting a merged deliverable needs the live maintainer; without one, keep
  it pending and log it. Silence never supplies approval.
- While any Goal is active, pass absolute `work_dir` on every built-in Booley
  MCP call that takes it: Flows, Specialists, Goal calls, `booley_targets` and
  `booley_report`. `booley_status`, `booley_poll`, `booley_cancel`, and
  `bwave` have no `work_dir` argument. For any other MCP call, follow its
  advertised schema. Independent non-Goal areas use the primary checkout root.
  Finish or explicitly abandon every Goal before closing its area and starting
  the next area. Schedule Interactive children only when no Goal is active in
  the same Project.
- From the host, run container commands as
  `booley session enter -- booley ...`; a Sandbox command written without that
  prefix is meant for the child's terminal inside the container.
- Before entry, copy the selected `goals/*.md` into the Main Booley
  Directory's `goalsets/` (`.booley/goalsets/` by default), and commit setup, seeded Goalsets and the managed `.gitignore`.
  `goals/*.json` are ad-hoc `goal_enter` argument lists. Every entered Goal is
  mandatory. Use unique slugs per run.
  Design implementations occur only in clean linked Goal worktrees.
- Operator integration always merges the outer `goal/<slug>-<date>` Goal
  Branch into its run-owned destination. If `booley worktree new` created a
  Paired Checkout (a Versioned Booley Directory), also merge its
  `booley-worktree/<name>` branch into its destination. Record in
  `resources.md` only the branches and checkouts actually created. A
  non-Stealth scaffold such as UART gets a Snapshot and has no paired branch.
  Preserve the Review Package, then remove the worktree with
  `booley session enter -- booley worktree rm <name>`, which removes a Paired
  Checkout first and keeps evidence and branches. Leave every participating
  destination clean before the next
  `booley session enter -- booley worktree new <name>`.
