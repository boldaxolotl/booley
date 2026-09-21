# Booley

Booley is a Python framework for agentic FPGA/ASIC development, including
simulation, synthesis, linting, and ticket workflows.

## Repository Rules

- Before modifying Booley code in a new chat, create a worktree and new branch
  from `main`, even when the existing checkout has dirty files.
- Before creating that worktree, run the Agent Readiness Check's `prepare`
  phase with the intended `codex/` branch. After creating it and before
  modifying files, run `develop` from the intended worktree:
  `python3 .github/scripts/agent_readiness.py` on POSIX or
  `py -3 .github/scripts/agent_readiness.py` on Windows. Treat its reported
  paths, statuses, escalation requirements, and blockers as authoritative.
  Exit zero is not permission to proceed when the aggregate status is
  `escalation-required`; resolve that escalation first. Run remediation only
  for failed checks and verification commands at their reported lifecycle
  point.
- Keep worktree branches local. Push a branch or create/update a pull request
  only on explicit request. A request to create/update a PR authorizes its
  required branch push; an implement, edit, or commit request does not.
- For every agent-authored PR creation, title/body edit, comment, or review,
  draft the public text in local files and submit it through
  `python3 .github/scripts/confidential_content_guard.py --repo . publish-pr`.
  It scans and sends the same bytes; see
  `docs/internals/agents/confidential-content.md` for each action.
- For every agent-authored issue creation, title/body edit, comment, or close
  comment, draft the public text in local files. Scan each draft with
  `python3 .github/scripts/confidential_content_guard.py --repo . pr-text --file <draft>`
  before submitting that same text with `gh`. Review PR and issue links and
  attachments for confidential facts the vocabulary cannot match.
- `main` is protected. Queue or merge a PR only when the user asks to merge it.
  Use the Mergify queue and read `docs/internals/agents/merge-queue.md` before
  queueing, dequeueing, retrying, monitoring, or cleaning up that PR. After
  Mergify reports an authorized merge, delete its local branch and worktree
  and its GitHub branch.
- Read `docs/internals/CODING_PRINCIPLES.md` before writing Python code.
- Ruff findings are repository work: during Python work, fix every finding
  reported for `src/` or `tests/`, including pre-existing or unrelated
  findings. Before committing, run the complete Ruff command reported by
  Agent Readiness and make it pass; a changed-files-only check is insufficient.
- Keep project-specific content in the directory resolved by `booley.runtime.project_dir`;
  framework code must not hardcode project paths or names.
- Keep implementation plans under `docs/plans/`. Plans are local-only artifacts
  and must not be committed to version control.

## Agent skills

- **Confidential vocabulary:** When changing banned terms or their CI
  configuration, follow `docs/internals/agents/confidential-content.md`.
- **Issue tracker:** GitHub Issues stores issues and specs. See
  `docs/internals/agents/issue-tracker.md`.
- **Triage labels:** Use the standard Matt Pocock labels. See
  `docs/internals/agents/triage-labels.md`.
- **Domain docs:** Read `CONTEXT-MAP.md`, then the glossary for each context the
  work touches. Optional ADR history may exist under `docs/adr/`. See
  `docs/internals/agents/domain.md`.
