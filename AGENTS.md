# Booley

Booley is a Python framework for agentic FPGA/ASIC development, including
simulation, synthesis, linting, and ticket-based workflows.

## Repository Rules

- In every new chat, before modifying Booley code, create a new worktree with a
  new branch based on `main`. Always isolate the work this way, especially when
  the existing checkout has dirty files.
- Keep worktree branches local by default. Push a branch or create or update a
  pull request only when the user explicitly requests that external action. A
  request to create or update a pull request authorizes its required branch
  push. A request to implement, edit, or commit does not authorize a push or
  pull request.
- `main` is protected. Queue or merge a pull request only when the user
  explicitly asks to merge it. Use the Mergify queue workflow for that merge;
  read `docs/internals/agents/merge-queue.md` before queueing, dequeueing,
  retrying, monitoring, or cleaning up that pull request.
- After Mergify reports an authorized pull request merged, delete its local
  branch and worktree, and delete its branch on GitHub.
- Read `docs/internals/CODING_PRINCIPLES.md` before writing Python code.
- Run `ruff check src/ tests/` before committing Python changes.
- Keep project-specific content in the directory resolved by `booley.runtime.project_dir`;
  framework code must not hardcode project paths or names.

## Agent skills

- **Issue tracker:** GitHub Issues stores issues and specs. See
  `docs/internals/agents/issue-tracker.md`.
- **Triage labels:** Use the standard Matt Pocock labels. See
  `docs/internals/agents/triage-labels.md`.
- **Domain docs:** Single-context: `docs/CONTEXT.md`; optional local ADR history
  may exist under `docs/adr/`. See `docs/internals/agents/domain.md`.
