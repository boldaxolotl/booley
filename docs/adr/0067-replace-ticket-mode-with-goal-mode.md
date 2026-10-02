# Replace Ticket Mode with Goal Mode

Status: proposed (2026-10-02)

Ticket Mode makes a human write a Ticket up front, queue it on the Ticket
Board, and review it later, with a separate Developer Agent doing the work.
In practice people explore interactively first and only then know what "done"
means. Goal Mode folds acceptance into Interactive Mode: at any point the
human tells the session's agent to enter Goal Mode, and from then on the work
is judged against machine-checked Goals until all are met. Ticket Mode, the
Ticket Board, and their CLI are removed; Goal Mode reuses Ticket Mode's
evidence machinery and is followed by a large dead-code removal.

## Decisions

- **One mode, no coexistence.** Goal Mode replaces Ticket Mode outright,
  including unattended work: an unattended Goal Mode is a tab left running.
  Keeping both would mean two acceptance systems that drift apart. No
  headless launch in v1.
- **Unit of work.** One agent session (one VS Code tab running Claude Code or
  Codex) holds at most one Goal Mode. Its record persists under the Project
  directory; continuing after a crash means resuming that same session. A
  fresh session cannot adopt it in v1.
- **Entry.** The session must be in a clean worktree (dirty: ask the human to
  commit first). Entry creates a new Goal Branch from HEAD; that HEAD is the
  base for baseline-relative Goals. Entry from the main checkout is refused.
  One worktree hosts at most one Goal Mode; any Interactive session sharing a
  worktree with another Booley session is warned.
- **Goals.** Criteria are renamed Goals. Every Goal is mandatory; optional
  Criteria are gone. Goals keep today's evidence rules: met only by Booley
  Flow or Specialist evidence, invalidated when the code they depend on
  changes. Evidence binds to file contents, so runs on uncommitted code
  count. Review Goals keep `clean` and `done`. A spec-review Goal names its
  spec file explicitly.
- **Goalsets.** Predefined, strictly-typed bundles of Goals replace prose
  Ticket Creation Guidance. Entry combines any Goalsets with ad-hoc Goals;
  on conflict the stricter Goal wins, with a warning.
- **Changing Goals.** The agent may propose adding or relaxing any Goal; the
  human approves each change in chat, and every change is logged. Coverage
  Waiver Candidates use the same relax-and-approve path. An agent needing a
  decision while unattended stops and asks in chat.
- **Protected inputs.** `.core` files and Booley bookkeeping are frozen at
  entry. Editing them warns immediately and blocks Finish until reverted.
  `.sdc` and `.xdc` are design files and stay editable; the review package
  flags their edits.
- **Scope dropped.** The review package's diff summary already shows every
  changed file.
- **Ending.** Finish requires every Goal met at the final commit, a clean
  tree, no unresolved protected-input violation, and a mandatory agent-written
  Session Summary. The agent finishes on its own; there is no human
  override. Finish presents the review package in chat: diff summary against
  the base, each Goal with its final evidence, the change log, open `done`
  findings, `.sdc`/`.xdc` edits, and the Session Summary; the HTML explainer
  only on request. Abandon ends Goal Mode and keeps the record marked
  abandoned. After either, the session may enter Goal Mode again, stacking a
  new Goal Branch.
- **No board, no merge action.** Board states, queueing, Ticket dependencies,
  Ticket Amendments, `on_success`, and the Acceptance Journal go. Merging is
  plain git, not a Booley concept. The record stays local in Stealth mode;
  otherwise a summary is also committed on the Goal Branch.
- **Entry surface.** An MCP tool enters Goal Mode; a guided skill wraps it.
  The entry result carries the Goal list and trimmed, high-value rules.
  Both ticket skills are retired.
- **Migration.** Hard removal: `booley run` and `booley board` become errors
  pointing at Goal Mode; committed Ticket History stays as inert files;
  Doctor warns about leftover board files.
- **Vocabulary.** Developer Agent, Ticket Mode, and Ticket Board retire.
  Escalation becomes Specialist → agent → human in chat.

## Open

- Target Plan (New, Replacement, Temporal Targets) needs a new shape.
- UI for live Goal status (met, unmet, stale).
- Content of the trimmed entry rules.
- Goalset syntax and location.

## Consequences

- QA suites need rework for Goal Mode.
- After Goal Mode lands, a dedicated pass removes dead Ticket Mode code: the
  Harness autonomy machinery (resume, auto-retry, orphan handling,
  subscription-limit waits), the Ticket Board lifecycle, and amendments.
