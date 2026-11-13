# Triage labels

The GitHub labels that carry an issue's triage state and category in this
repository. Agents read it when a skill (`triage`, `to-tickets`, the issue
pipeline, `booley-qa-triage`) names a triage role or category. How to run
`gh` against issues is in [issue-tracker.md](issue-tracker.md); what a
`ready-for-agent` issue needs is in [agent-brief.md](agent-brief.md).

## State labels

Each label has the same name as the triage skill's role.

| Label             | Meaning                                    |
| ----------------- | ------------------------------------------ |
| `needs-triage`    | The maintainer needs to evaluate the issue |
| `needs-info`      | Waiting for information from the reporter  |
| `ready-for-agent` | Fully specified, with an Agent Brief       |
| `ready-for-human` | Needs a human to implement it              |
| `wontfix`         | Will not be worked on                      |
| `tracker`         | Parent issue that tracks its sub-issues    |

`tracker` is Booley's state for a parent whose work was split into
sub-issues ([One PR per issue](agent-brief.md#one-pr-per-issue)). Whoever
files the sub-issues puts it on the parent in place of the parent's state
label: triage after its own split, or the issue pipeline after its planner
splits a `ready-for-agent` issue. Sweeps for issues that need attention then
skip the parent. A tracker has no Agent Brief and is never `ready-for-agent`.
Close it when its last sub-issue closes.

The issue pipeline never admits an issue labeled `needs-triage`, `needs-info`,
or any `wayfinder:` label. While a ticket waits on a maintainer question,
labeling the issue `needs-triage`, `ready-for-human`, or `wontfix` takes it
back: the pipeline stops the ticket.

## Category labels

| Label           | Meaning                          |
| --------------- | -------------------------------- |
| `bug`           | Something is broken              |
| `enhancement`   | A new feature or an improvement  |
| `documentation` | A change to documentation only   |

`documentation` is a third category beyond the triage skill's two. A
`documentation` change edits only `*.md` files and files under `docs/`, and
may rerun an existing generator; the pipeline refuses a `documentation` branch
that changes any other file. Text generated from code, text a guard test
keeps in sync with code, and a requested guard test all need code changes,
so they are `bug` or `enhancement`.

A code change that also updates docs is `bug` or `enhancement` and may carry
`documentation` as well. When an issue has several category labels, the
pipeline uses the strongest: `bug`, then `enhancement`, then `documentation`.
The category decides which models work the ticket: the pipeline works
`documentation` tickets with Claude only and never runs a GUI check on them.
