# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all
operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --comments`, also fetching labels.
- **List issues**: use `gh issue list` with appropriate state and label filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or
  `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`; `gh` does this automatically inside
the clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. Resolve an
ambiguous number with `gh pr view <number>` and fall back to
`gh issue view <number>`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The map is one issue with child issues as decision
tickets.

- **Map**: an issue labelled `wayfinder:map` containing Destination, Notes,
  Decisions so far, Not yet specified, and Out of scope.
- **Child ticket**: a GitHub sub-issue linked to the map. If sub-issues are
  unavailable, add it to a task list in the map and place `Part of #<map>` in
  the child body.
- **Ticket labels**: `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, or `wayfinder:task`.
- **Blocking**: use GitHub's native issue dependencies. If unavailable, use a
  `Blocked by: #<number>` line in the child body.
- **Frontier**: the map's open, unblocked, and unassigned child issues.
- **Claim**: assign the issue to the driving developer before starting work.
- **Resolve**: record the answer in a comment, close the issue, and add a
  concise linked pointer to the map's Decisions-so-far section.

## Agent claims

An agent that takes an issue claims it with a task-specific comment, then
assigns the issue to the authenticated login. Draft and scan the comment as
`AGENTS.md` requires, and post exactly:

```text
Claimed by agent `<agent-id>`.
```

The earliest claim that has not been released wins, ordered by `createdAt`,
then comment ID. Read every page of comments and the assignees before
assigning and before each later state-changing command; stop when a claim or
assignee appears that the last read did not show. These reads detect a
conflict. They are not a lock: two hosts can still race.

Release a claim only after every command the claiming run started has stopped
and no PR of that run is still queued. Post a release comment that names the
original claim comment ID, the run, and the reason, then remove the run's own
assignment:

```text
Released claim <claim-comment-id> by agent `<agent-id>`: <reason>.
```

GitHub authenticates the login, not the agent ID written in a comment. A
release therefore counts only when the releasing run can match it to its own
recorded command and the returned comment ID, or when the maintainer
authorizes the release or handoff. A release comment alone, written under a
shared login by an agent that cannot show that record, does not free the
claim. Elapsed time never releases a claim.

An open PR for the issue keeps it out of automatic selection even after its
claim is released. To recover an abandoned claim, the maintainer confirms that
the old run and its processes have stopped, then authorizes the takeover.
