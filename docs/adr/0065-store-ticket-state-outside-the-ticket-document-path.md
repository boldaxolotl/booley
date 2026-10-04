# Store Ticket state outside the Ticket document path

Status: accepted (2026-09-29; amended 2026-09-30: close and history commit run outside the Acceptance Journal)

A Ticket's lifecycle state used to be the `tickets/board/<state>/` directory its
Markdown lived in, and `ticket_board/lifecycle.py` documented that directory as
the single atomic source of truth. That made the document path change on every
transition. Because the board was also tracked by the inner project repository,
every Ticket worktree (a `git worktree` of that repository) carried a stale
board copy, board moves dirtied the tree and needed a special exemption
(`git_ops._is_allowed_unstaged_rename`), and archiving deleted the document, so
no record of finished work survived and waiting dependents of an archived
dependency were stranded. We now keep each live Ticket at the stable path
`tickets/board/<slug>.md` and store its state in an ignored per-Ticket record,
`tickets/state/<slug>.json`. That record is the compare-and-swap target under
the existing per-Ticket lock; a single atomic replace keeps the same atomicity
the directory rename gave. Closed Tickets (done or archived) move once to the
tracked, append-only `tickets/history/<slug>.md`.

## Decisions

- **Git surface.** `board/`, `state/`, `logs/`, and `locks/` are ignored;
  only `history/` is tracked. The Ticket Board is therefore local to one
  Project checkout. A clone starts with an empty board plus the full history,
  and nested Ticket worktrees see only history, which never changes.
- **State record.** `state/<slug>.json` holds the state, execution identity,
  and the former `.runtime/progress.json` runtime fields. It lives outside
  `logs/` so that pruning logs can never erase lifecycle authority.
  `transitions.log` remains a runtime-only log and is not tracked.
- **Draft.** A document with no state record is a draft. Creating a draft
  writes only the document. Return-to-draft deletes the record, and enqueue
  creates it.
- **Closing.** Both done and archived close a Ticket. The move writes
  `history/<slug>.md` with a machine-only `closed: {outcome, date, generation}`
  block, then removes `board/<slug>.md`, then deletes the state record. If a
  crash leaves copies behind, presence in `history/` wins. For done, the move
  runs right after the Acceptance Journal reports the acceptance complete, not
  as a journal step: the close is idempotent, and every Ticket Board
  operation's reconciliation closes a done Ticket whose close a crash lost.
  Archive releases the Ticket's worktrees and refs before it closes; a done
  close leaves them to completion's cleanup policy, so `--no-cleanup` keeps
  them for inspection. Logs are kept.
- **Publishing history.** Booley commits each history record to the
  repository that tracks `history/` itself
  (`chore(<slug>): close Ticket (<outcome>)`). The commit is built in a
  private index and published with a compare-and-swap `update-ref` on the
  checked-out branch, under the acceptance publication lock. It keeps no
  journal: Git state is the recovery record, because a record the branch
  already holds needs nothing and any other pending record is retried on the
  next Ticket Board operation. When the Ticket names a
  `project_destination_ref` that is not the checked-out branch, the commit is
  refused and retried later rather than landing on the wrong branch. Doctor
  WARNs about uncommitted records and about an ignored `history/`.
  A Ticket counts as closed once the history record exists
  in the working tree; the commit is recoverable follow-up work, never a
  precondition.
- **Other two-file transitions.** Enqueue (the document gains its machine
  section, and the state record is created) and return-to-draft (the machine
  section is stripped, and the state record is deleted) are ordered and
  journaled so that a crash never leaves an executable-form document without
  a state record, or a draft-form document with one.
- **Failing closed.** Only an absent state record means draft. A record that
  cannot be read, cannot be parsed, has an unknown schema, or has invalid
  fields makes every command on that Ticket fail without changing anything.
- **Archive.** `archive` becomes the abandon verb for live Tickets only.
- **Immutability.** Closed Tickets are never reopened. Slugs are unique
  across `board/` and `history/`.
- **Dependencies.** A dependency is satisfied if and only if its history record
  has outcome done. An archived dependency blocks its waiting dependents
  lazily: waiting promotion reads history and blocks each dependent it finds.
  This check is idempotent, so no fan-out runs at archive time and a crash
  cannot leave dependents half-updated.
- **Legacy layout.** Doctor FAILs, and board commands refuse to run, while
  files remain under the old `board/<state>/` directories. Existing projects
  migrate by hand. There is no migration code and no dual-layout reads.

## Considered options

- **State in frontmatter.** Rejected: it puts lifecycle churn back into a
  tracked file.
- **SQLite.** Rejected: its locking is risky on host and container bind
  mounts, and per-Ticket files already have per-Ticket locks.
- **Board on an orphan branch.** Rejected: it is too clever to maintain.
- **Tracking `board/` too.** Rejected: nested Ticket worktrees would keep
  stale live Tickets.
- **History tracked but committed by the user.** Rejected: nobody would own
  the commit, so the working tree and the project history would drift apart.
