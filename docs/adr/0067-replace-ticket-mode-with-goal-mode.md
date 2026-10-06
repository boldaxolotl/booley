# Replace Ticket Mode with Goal Mode

Status: proposed (2026-10-02; amended 2026-10-06: Phase 0 spike results and plan decisions, see Amendments)

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
  worktree with another Booley session is warned, using the session
  registry of ADR 0068.
- **Goals.** Criteria are renamed Goals. Every Goal is mandatory; optional
  Criteria are gone. Goals keep today's evidence rules: met only by Booley
  Flow or Specialist evidence, invalidated when the code they depend on
  changes. Evidence binds to file contents, so runs on uncommitted code
  count. Review Goals keep `clean` and `done`. A spec-review Goal names its
  spec file explicitly.
- **Goalsets.** Named Goal bundles replace Ticket Creation Guidance. Each
  Goalset is one free-form Markdown file in `.booley_project/goalsets/`,
  written in the vocabulary of the entry tool's Goal arguments; the agent
  translates the chosen Goalsets into those arguments, so validation
  happens at entry, not on the file. `init` seeds `feature`, `bugfix`,
  `refactor`, and `verification` Goalsets from today's Criteria templates
  only when missing; afterwards the Project owns them and Booley ships no
  runtime built-ins. A Goalset named `default.md` applies to every entry
  unless the human explicitly skips it, which the change log records.
  Goalsets are read only at entry, so they are not protected inputs. Entry
  combines any Goalsets with ad-hoc Goals; on conflict the stricter Goal
  wins, with a warning. The entry tool takes concrete Goals only: every
  per-Target Goal names its Target, and the agent does any expansion.
- **Changing Goals.** The agent may propose adding or relaxing any Goal
  through an MCP tool, and every change is logged. Where the client
  supports MCP elicitation, the tool asks the human directly, so the agent
  never records an approval; the form requires a typed reason, which
  defeats empty-form auto-accept and feeds the change log. A client hook
  that answers the form is the user's own configuration, not detected. Otherwise the agent records the approval with
  the human's quoted words, and the review package marks it as
  agent-recorded. Coverage Waiver Candidates use the same relax-and-approve
  path. An agent needing a
  decision while unattended stops and asks in chat.
- **Protected inputs.** Files that decide how evidence is produced are frozen
  at entry: `booley.toml`, `.booley_project/{hooks,.managed,generators,mcp_tools}/`,
  and `FUSESOC_IGNORE`. Custom Flows are protected because they produce
  evidence like built-in Flows. Editing a protected input warns immediately
  and blocks Finish until reverted. Design files stay editable and are
  flagged in the review package instead: `.core`, `tests.toml`, `.sdc`, and
  `.xdc`.
- **Targets.** The Target Plan and its New, Replacement, and Temporal roles
  go; Targets are ordinary design inputs, and removing leftover Targets is
  plain git like merging. A Goal bound to a Target that is renamed or
  deleted stays unmet until the Target is restored or a human approves a
  retargeting Goal change; Booley does not track renames. A Goal may name a
  candidate Target that does not exist yet: entry warns, and the Goal stays
  unmet until the Target exists. A baseline-relative Goal names a baseline
  Target (by default the candidate's name) that must exist at the base
  commit, and is judged against that Target as defined at the base commit,
  run on base code.
- **Scope dropped.** The review package's diff summary already shows every
  changed file.
- **Ending.** Finish requires every Goal met at the final commit, a clean
  tree, no unresolved protected-input violation, and a mandatory agent-written
  Session Summary. The agent finishes on its own; there is no human
  override. Finish presents the review package in chat: diff summary against
  the base, each Goal with its final evidence, the change log, open `done`
  findings, Target changes, `.sdc`/`.xdc` edits, and the Session Summary;
  the HTML explainer only on request. Target changes lists Targets added,
  removed, or modified, with a semantic diff of parameters, defines,
  toplevel, fileset membership, and test entries; a modified Target that a
  Goal binds to is marked beside that Goal's evidence. Abandon ends Goal
  Mode and keeps the record marked abandoned; the agent abandons only on
  the human's explicit instruction. A human clears a crashed, unresumed
  Goal Mode with `booley goal abandon` in its worktree; Doctor warns about
  a Goal Mode whose session has gone quiet but never clears it. After either, the session may
  enter Goal Mode again, stacking a new Goal Branch.
- **No board, no merge action.** Board states, queueing, Ticket dependencies,
  Ticket Amendments, `on_success`, and the Acceptance Journal go. Merging is
  plain git, not a Booley concept. The record stays local in Stealth mode;
  otherwise a summary is also committed on the Goal Branch.
- **Entry surface.** An MCP tool enters Goal Mode; a guided skill wraps it.
  The entry result carries the Goal list and these rules: only Booley Flows
  and Specialists produce evidence; editing a protected input (listed)
  blocks Finish; every Goal is mandatory, and adding or relaxing one needs
  the human's approval in chat; never weaken tests or Targets to meet a
  Goal, since `.core` and `tests.toml` edits are flagged; Finish needs every
  Goal met at a clean, committed HEAD plus a Session Summary; check
  `goal_status` when unsure. `goal_status(rules=true)` returns the rules
  again after context compaction. Projects add their own rules in `AGENTS.md`,
  not through Booley. Both ticket skills are retired.
- **Goal status.** The agent pulls status with the `goal_status` MCP tool;
  Flow and Specialist results carry no Goal delta. The human watches live
  status in the Booley Dashboard (ADR 0068). `booley goal status` prints a
  snapshot: inside a worktree hosting a Goal Mode it shows that one;
  elsewhere it shows every active Goal Mode in the Project. It defaults
  to the long view for one Goal Mode and the short view for several;
  `--short` and `--long` override. Each Goal shows met, unmet, or stale, without naming the
  change that made it stale.
- **Migration.** Hard removal: `booley run` and `booley board` become errors
  pointing at Goal Mode; committed Ticket History stays as inert files;
  Doctor warns about leftover board files and about a leftover
  `ticket_creation.md` or `ticket_defaults.md`, suggesting its content move
  into a Goalset; `init` stops scaffolding it.
- **Vocabulary.** Developer Agent, Ticket Mode, and Ticket Board retire.
  Escalation retires too: the agent asks the human in chat.


## Consequences

- QA suites need rework for Goal Mode.
- After Goal Mode lands, a dedicated pass removes dead Ticket Mode code: the
  Harness autonomy machinery (resume, auto-retry, orphan handling,
  subscription-limit waits), the Ticket Board lifecycle, and amendments.
  It also removes Target Plan validation, acceptance-time Target removal,
  and planned-dependency Target exports. The semantic `.core` and
  `tests.toml` comparison survives to build the Target changes section.


## Amendments (2026-10-06)

Recorded after the Phase 0 spikes
([elicitation](../internals/validation/0067-goal-mode-elicitation.md),
[session identity](../internals/validation/0067-goal-mode-session-identity.md),
[Project directory](../internals/validation/0067-goal-mode-project-dir.md)).

- **Changing Goals, elicitation.** Both supported clients speak the
  2026-07-28 wire and declare form elicitation. The proposal tool returns
  `input_required` with the decision/reason form; a retry whose response
  is `accept` with content is an elicited decision, approve or reject as
  `content.decision` says. Claude Code renders the form to the human; in
  every exchange tested Codex auto-declined without showing it. On
  `decline`, `cancel`, no response, or invalid content the tool returns
  `approval_required` and the agent-recorded path applies, which is where
  Codex sessions landed each time. The decision is read from the request's
  `input_responses` only, never from tool arguments, and the request state
  is sealed; neither proves a human answered, so the change log records the
  peer process of the retry and the review package shows a mismatch with
  the session's own process. Server-initiated `elicitation/create` has no
  channel on this wire and is not used.
- **Unit of work.** The worktree owns a Goal Mode. The sentence "A fresh
  session cannot adopt it in v1" is replaced by: any session working in the
  Goal worktree may read status, propose changes, and finish; the record
  stores the entering and acting session keys as audit fields only.
- **Entry, Goal Branch.** Entry creates the Goal Branch in place
  (`git checkout -b goal/<slug>-<date>` in the session's worktree, refusing
  an existing name). Any clean HEAD is a valid base, including a detached
  one or a previous Goal Branch; the record stores the base commit and the
  branch HEAD was on.
- **Record and summary.** The local record lives in the Project directory
  that discovery returns, under `.booley_project/goals/<goal-id>/`. Outside
  Stealth, the committed summary is a separate file written inside the Goal
  worktree at `.booley_project/goals/history/<goal-id>.md` and committed
  there with the branch bound to the Goal Branch (a different checkout
  refuses); the repository is the worktree's, never the Project
  directory's. Under Stealth the record stays local, as above. `init` keeps its `/.booley_project` exclude line; the
  Project `.gitignore` ignores `goals/*/` and keeps `goals/history/`.
- **Goal conflicts.** Goals that differ in a setting with no defined order
  (different baseline Target, coverage test selection, review spec file,
  mutation scope/total/auto) are refused at translation; nothing is merged
  silently.
- **Goalsets.** `init` seeds only the four named Goalsets; it does not seed
  `default.md`. With no `default.md` the entry check passes and the skip
  flag must be false; `booley-setup` offers to create one.
- **Rollout.** Goal tools, `booley goal`, `booley dashboard`, and the
  `booley-goal` skill are registered only when `BOOLEY_GOAL_MODE_PREVIEW=1`
  is set until the release that removes Ticket Mode; "no coexistence" is
  enforced on registration, not on files.
