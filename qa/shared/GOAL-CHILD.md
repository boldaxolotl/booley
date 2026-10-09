# Goal-child operating rules

Apply these rules to every mission area that uses a Goal child.

- The host operator starts a long-lived Codex Goal child in the Sandbox with
  `booley session enter -- codex ...`, then sends `$booley-goal` and the area's
  mission prompt. Claude children use `/booley-goal`. The operator never
  implements the child's design work. Read
  `src/booley/data/skills/booley-goal/SKILL.md` (including `review.md`) and
  `docs/user/USAGE.md` "Goal Mode" from the candidate build for the workflow.
- The operator is the deciding human for disposable QA proposals: answer entry
  choices and approve or reject the exact proposal with a reason, through the
  client form or a message the child quotes as `approval_quote`. A proposal
  affecting a merged deliverable needs the live maintainer; without one, keep
  it pending and log it. Silence never supplies approval.
- While any Goal is active, every MCP Booley call passes absolute `work_dir`;
  independent non-Goal areas use the primary checkout root. Finish or explicitly
  abandon every Goal before closing its area and starting the next area.
  Schedule Interactive children only when no Goal is active in the same Project.
- Host-issued container commands use `booley session enter -- booley ...`;
  bare Sandbox commands below are for the child/container terminal.
- Before entry, copy the selected `goals/*.md` into the resolved Project's
  `goalsets/`, and commit setup, seeded Goalsets and the managed `.gitignore`.
  `goals/*.json` are ad-hoc `goal_enter` argument lists. Every entered Goal is
  mandatory. Use unique slugs per run.
  Design implementations occur only in clean linked Goal worktrees.
- Operator integration always merges the outer `goal/<slug>-<date>` Goal
  Branch into its run-owned destination. If `booley worktree new` printed a
  paired checkout (the Project directory is its own Git repository), also merge
  its `booley-worktree/<name>` Project-data branch into its destination. Record
  only the branches/checkouts actually printed in `resources.md`. A non-Stealth
  scaffold such as UART has no paired branch. Preserve the package and use the
  printed USAGE cleanup steps; remove a paired checkout first when present.
  Leave every participating destination clean before the next
  `booley session enter -- booley worktree new <name>`.
