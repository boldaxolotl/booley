# Booley

Booley is a Python framework for agentic FPGA/ASIC development, including
simulation, synthesis, linting, and ticket-based workflows.

## Repository Rules

- In every new chat, before modifying Booley code, create a new worktree with a
  new branch based on `main`. Always isolate the work this way, especially when
  the existing checkout has dirty files.
- `main` is protected; submit every change through a pull request from its
  worktree branch.
- After merging a pull request, delete its local branch and worktree, and delete
  its branch on GitHub.
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
