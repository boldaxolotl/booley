# Agent Briefs

An Agent Brief is the contract an unattended agent, such as the issue
pipeline, works from: it says what the finished change must do. The issue
body stays the context: what happened, the repro, the build identity, the
verified cause with `file:line` evidence, and the discussion. Read this
before writing a brief (in `triage`, `booley-qa-triage`, `to-tickets`, or a
pipeline split) and when the pipeline refuses one. It extends the triage
skill's brief format with Booley's fields, placement, and authority rule.
Labels are in [triage-labels.md](triage-labels.md); `gh` commands are in
[issue-tracker.md](issue-tracker.md).

## Readiness

Write a brief only for an issue with no open scope, behavior, or contract
question. Label an issue that still has one `needs-info` or `needs-triage`
and post no brief.

## Write it durable

The issue may wait for weeks while the code moves, so keep the brief
behavioral:

- Describe behavior, interfaces, commands, types, and config shapes.
- Say what the system must do, not how to implement it.
- Leave out file paths and line numbers; they belong in the issue body. The
  pipeline refuses a split brief that names one.

## Fields

Write each field as a bold label at the start of a line, in this order. The
pipeline reads a field from its label to the next label or heading, and a
`#` or `##` heading ends the brief, so put no headings inside it.

Required:

- `**Category:**` `bug`, `enhancement`, or `documentation`. It must equal the
  issue's category label; see
  [Category labels](triage-labels.md#category-labels) for which one applies.
- `**Summary:**` one line: what must happen.
- `**Current behavior:**` what happens now. For a bug, the broken behavior.
- `**Desired behavior:**` what happens after the change, including edge cases
  and error conditions.
- `**Acceptance criteria:**` a checkbox list (`- [ ]`). Each item is
  independently verifiable. Indent a continuation line under its item.
- `**Out of scope:**` what the agent must not change.

Optional:

- `**Key interfaces:**` commands, types, functions, or config keys the change
  touches, named without paths.
- `**Audit:**`, `**Review:**`, `**GUI:**`: see below.
- `Blocked by: #<n>` lines, which count as much as the issue body's.

## Audit

Add `**Audit:**` when the defect or gap may exist elsewhere, that is, when
triage rates the blast radius yes or likely. Name the pattern to search for
and the suspected places by concept, such as the other Booley Flows or sibling
commands. Add an acceptance criterion that every place the audit finds is
fixed.

## Review

By default the pipeline reviews a plan and its pull request once each, with
its standard reviewer models. Add `**Review:**` when a wrong result would be
hard to detect after merge, so the work needs a stronger reviewer and repeated
review: for example, a cache whose hits count as Goal evidence, where a wrong
hit looks like a pass.

The value is one line of `key=value` pairs, split with shell quoting rules:
quote any value that contains spaces.

```markdown
**Review:** model=claude-fable-5-1 effort=high rounds=uncapped park-after=5 focus="no wrong cache hit"
```

- `model` (required): `claude-fable-5-1` or `claude-opus-5-5`. It replaces
  the plan reviewer and the Spec and adversarial code reviewers. The
  Standards reviewer, the planner, and the implementer keep their models.
- `effort` (required): `low`, `medium`, `high`, or `xhigh`.
- `rounds`: `1` (default) or `uncapped`. With `uncapped`, every fix starts
  another review of the whole change, until a round has no open finding rated
  `medium` or higher on the pipeline's shared severity scale. A `low` or
  `nit` finding is fixed or declined without another round. A `medium` or
  higher finding closes only through a fix, or through a written rebuttal the
  next round's reviewer accepts. A proposed split still gets one round.
- `park-after`: an integer of at least 2, only with `rounds=uncapped`;
  default 5. After that many rounds under the current brief without a clean
  one, the pipeline parks the ticket on a maintainer question. The
  maintainer's answer may accept the parked head with its findings open.
- `focus`: one quoted line that tells the reviewer what to look for first.

With `rounds=uncapped`, the pipeline queues the pull request only after a
round with no open `medium` or higher finding on its current head, or after
the maintainer's answer to a park accepts that exact head.

A malformed field keeps the issue out of the pipeline instead of falling back
to the default review, because a silent fallback would weaken exactly the
review the brief asked for. Malformed means an unknown key or model, a
repeated key, a missing `model` or `effort`, an unsupported `rounds` value,
`park-after` without `rounds=uncapped`, a value over several lines, or
unbalanced quotes.

## GUI

The pipeline checks a pull request that touches the GUI in a browser
Codespace with the `booley-gui-check` skill, and queues it only after a
passing check on its current head. Without this field, the pipeline's path
rules decide: changes to the VS Code extension data, the Dashboard,
`bwave gui` and VaporView, the in-container runtime, or the published image
and devcontainer recipes need a check. The Spec reviewer can flag a change
the rules missed.

Add `**GUI:**` only when the rules would get the ticket wrong:

- `**GUI:** yes`: the change needs a check though it touches no GUI path, for
  example a CLI change that alters what the Dashboard shows.
- `**GUI:** no`: the change touches a GUI path but changes nothing a user
  sees, for example a refactor of an in-container module.

Any value other than `yes` or `no` keeps the issue out of the pipeline.
`documentation` tickets never run the check.

## Placement and authority

Post the brief as an issue comment by the issue author. The pipeline admits
only issues the maintainer's login opened, so post under that login. The
comment starts with the triage skill's AI-disclaimer line, then a
`## Agent Brief` heading, as in the [example](#example).

- The newest brief comment by the issue author, by creation time, is
  authoritative.
- A `## Agent Brief` section in the issue body counts only when no brief
  comment exists.
- Comments by anyone else never count.
- To change the contract, post a new brief comment instead of editing the
  current one. The pipeline records a hash of the brief's text, so any edit
  after work started sends the ticket back to planning.

## One PR per issue

A brief covers work that fits one pull request: one coherent change that can
merge on its own, be reviewed in one pass, and stay within the size caps of
[coding principle 18](../CODING_PRINCIPLES.md#18-pull-requests-stay-small).
When the work does not fit, split it before it becomes `ready-for-agent`:

- Create one sub-issue per piece, each with its own brief, and order them with
  `Blocked by: #<n>` lines ([Sub-issues and
  blocking](issue-tracker.md#sub-issues-and-blocking)).
- Draw each sub-issue's acceptance criteria from the parent's. Together, the
  sub-issues cover all of them.
- Label the parent [`tracker`](triage-labels.md#state-labels). It gets no
  brief.

The pipeline skips an issue that has open sub-issues. When its planner finds
a `ready-for-agent` issue too large for one PR, the pipeline splits it without
asking: it files a sub-issue per piece with its brief in an author comment
and the `ready-for-agent` label, posts a split table on the parent, relabels
the parent `tracker`, and works the sub-issues in order.

## Example

```markdown
> *This was generated by AI during triage.*

## Agent Brief

**Category:** bug
**Summary:** `booley flow lint` reports PASS when the selected linter is missing

**Current behavior:**
When the Target's lint EDA tool is not installed in the Sandbox,
`booley flow lint` logs a launch error but exits 0 and reports PASS.

**Desired behavior:**
A missing linter fails the run with a nonzero exit and a message that names
the missing executable. A linter that runs and reports findings is unchanged.

**Key interfaces:**
- The Lint Flow verdict: status and exit code

**Audit:**
Search every Booley Flow for an EDA tool launch failure that maps to success.
Suspected: the Simulation and ASIC Synthesis Flows, which launch their EDA
tools the same way.

**Acceptance criteria:**
- [ ] `booley flow lint` with a missing linter exits nonzero and names the
      executable
- [ ] A test covers the missing-linter case
- [ ] Every launch-failure-as-success place the audit finds is fixed and tested

**Out of scope:**
- Installing or auto-discovering linters
- Changing lint finding severities
```
