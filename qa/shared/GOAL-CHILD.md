# Goal-child operating rules

Apply these rules to every mission area that uses a Goal child.

- The host operator starts a long-lived Codex Goal child in the Sandbox with
  `booley session enter -- codex ...`, then sends `/booley-goal` and the area's
  mission prompt. The operator never implements the child's design work. Read
  `src/booley/data/skills/booley-goal/SKILL.md` (including `review.md`) and
  `docs/user/USAGE.md` "Goal Mode" from the candidate build for the workflow.
- The operator is the deciding human for disposable QA proposals: answer entry
  choices and approve or reject the exact proposal with a reason, through the
  client form or a message the child quotes as `approval_quote`. A proposal
  affecting a merged deliverable needs the live maintainer; without one, keep
  it pending and log it. Silence never supplies approval.
- While any Goal is active, every MCP Booley call passes absolute `work_dir`;
  independent non-Goal areas use the primary checkout root. Finish or explicitly
  abandon every Goal before closing its area. Background children may overlap
  independent operator areas; keep their owning area open until resolved.
- Host-issued container commands use `booley session enter -- booley ...`;
  bare Sandbox commands below are for the child/container terminal.
- Before entry, copy the selected `goals/*.md` into the resolved Project's
  `goalsets/`, and commit setup, seeded Goalsets and the managed `.gitignore`.
  `goals/*.json` are ad-hoc `goal_enter` argument lists. Every entered Goal is
  mandatory. Use unique slugs per run.
  Design implementations occur only in clean linked Goal worktrees.
- Operator integration of finished work merges the outer `goal/<slug>-<date>`
  Goal Branch and the paired `booley-worktree/<name>` Project-data branch into
  their respective run-owned destinations. Record both in `resources.md`.
  Preserve the package, remove the paired checkout first using the printed
  USAGE cleanup steps, and leave both destinations clean in the primary
  checkout before `booley session enter -- booley worktree new <name>` again.
