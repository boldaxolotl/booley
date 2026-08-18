# Design Grilling (detailed-plan mode only)

Read this file **only** when the user picked depth 2 ("Detailed plan") in SKILL Step 2a.
Lightweight tickets skip it entirely and go straight to field inference (SKILL §B).

Grill the user on their design to surface missing details, unresolved decisions, and edge
cases, then distil the results into a written implementation plan. Both the plan and the
field inference feed off this.

## Workflow

1. Read the user's initial input. Infer `type`; identify what's clear vs. vague. Map the
   design as a decision tree: each unresolved decision branches into the decisions that
   depend on it.
2. Find facts instead of asking the user for them. Inspect the codebase, specs, tests,
   documentation, filesystem, and available MCP tools. When independent agents are available,
   delegate unrelated factual investigations so research can proceed in parallel. Treat
   running research as an unsettled prerequisite for the decisions that depend on it.
3. Work through the tree in rounds. The **frontier** is every unresolved decision whose
   prerequisites are already settled. Ask the whole frontier in one round, then wait for
   the user's answers. If a question depends on another question that is still open in the
   current round, defer it to a later round.
4. After each response, record the settled decisions and recompute the frontier. Answers
   expose downstream questions; unanswered questions remain open rather than being silently
   inferred.
5. Finish only when the frontier is empty and no branch remains silently assumed. Summarize
   the resulting shared understanding and ask the user to confirm it.
6. After confirmation, write the detailed plan (see below). Include any explicitly deferred
   follow-up, its owner or fallback, and the impact of leaving it open.

Format every question like this:

```md
❓ **Q1** - **<question title>**: <question body, including choices when useful>

➡️ <recommended answer and why>
```

## What to Probe

Scale depth to complexity; skip what the user already covered.

### All Types

| Area | Planning impact |
|------|-----------------|
| Interface contracts (ports, widths, handshake) | Signal-level scope |
| `ifdef` / config interaction; which configs matter | Config lists for lint/sim criteria |
| Existing tests — which pass, which should change | `sim_pass` criteria (`pass->pass` vs `fail->pass`) |
| Scope completeness, edge cases, hidden breakage risks | Prevents scope creep; surfaces dependencies |
| Verification strategy & coverage gaps | TB criteria and review focuses |
| Dependencies & ordering risks | `dependencies` field |

### Feature-Specific

| Area | Why |
|------|-----|
| FSM / control path completeness | Complete state coverage |
| Timing assumptions & pipeline staging | RTL structure decisions |
| Area vs timing tradeoffs | May trigger `synthesis_ok` |

### Bugfix-Specific

| Area | Why |
|------|-----|
| Reproduction steps — config, test, exact failure | Agent's starting point; populates Failing Simulation |
| Observed symptoms vs expected behavior | Populates Observed Symptoms |
| Suspected root cause | Focuses the developer's investigation |
| Whether a test exists yet | May split: feature (add test) + bugfix (fix RTL) |
| Known-good commit or config where the test passes | Lets agent `git diff` to isolate regression; narrows scope dramatically |

### Refactor-Specific

| Area | Why |
|------|-----|
| Behavioral invariants — what must NOT change | `pass -> pass` criteria |
| CDC & reset affected by restructuring | Silent breakage risk |

### Verification-Specific

| Area | Why |
|------|-----|
| Coverage gaps — which behaviors/states are untested | Drives new test scenarios |
| Stimulus strategy — constrained random vs directed | TB architecture decisions |
| Checking strategy — assertions vs scoreboard vs golden | TB quality criteria |
| RTL boundary — what NOT to touch | Verification must not modify RTL |

## Rules

- **Never re-ask** what the user already stated.
- **Codebase first** — read code/specs/tests instead of asking when possible.
- **One frontier per round** — ask all currently unblocked questions together, each with a
  recommended answer; do not mix in downstream questions.
- **Keep decisions with the user** — show assumptions and material trade-offs, then wait for
  an answer instead of choosing silently.
- **Proportional depth** — 2–3 questions for a simple refactor, 10+ for a new pipeline stage.

## Writing the Detailed Plan

Once the user confirms the shared-understanding summary, synthesise the decisions into a
concrete implementation plan and place it in the ticket body under
`## Implementation Plan`, **after** the type-specific `## Description` block. The section
skeleton (Approach / Implementation Steps / Interface Changes / Edge Cases & Risks /
Verification / Open Questions) lives in `TICKET_TEMPLATE.md` — fill that, don't invent a
different shape.

This is what the developer plans against, so it must be actionable — no restating of the
summary, no vague "improve X". Scale each section to the work:

- **Approach** — the chosen design and the key decisions from grilling, with rejected alternatives noted where they matter.
- **Implementation Steps** — ordered, file-by-file breakdown of what to change and in what order: the sequence a coder would follow.
- **Interface Changes** — new/changed ports, signals, widths, handshakes, config `ifdef`s.
- **Edge Cases & Risks** — corner cases, reset/CDC concerns, hidden breakage, and how each is handled.
- **Verification** — how the change is proven: which TBs/tests, new scenarios, what "done" looks like (ties back to the criteria in SKILL §D).

Keep it in prose + short lists, not a wall of headings. Fold the grilling outcomes in
directly — there is no separate "Grilling Results" section. Open questions that survived
grilling by explicit agreement go at the end under **Open Questions**, with their owner or
fallback and impact, not silently dropped.
