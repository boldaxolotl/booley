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
