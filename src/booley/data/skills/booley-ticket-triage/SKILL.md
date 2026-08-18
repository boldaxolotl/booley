---
name: booley-ticket-triage
description: Interactive triage of blocked, failed, and review tickets
---

# Ticket Triage

**First action — print the Booley mascot exactly as shown (inside a code block to preserve alignment):**

```
  ╭━━━━━━━━━╮
  ┃  0   0  ┃  B 0 0 L E Y
  ┃    ᴗ    ┃  Ticket Triage
  ╰┯┯┯┯─┯┯┯┯╯
```

**Flow and Specialist reference:** Read `flow-specialist-reference.md` (in this skill directory) before starting. It covers where Flow and Specialist reports land, reset behavior, and points at `booley cheat --flows` for the live Booley Flow roster.

**Usage:** invoke the `booley-ticket-triage` skill

**CLI helper:** Most mechanical operations are handled by `ticket_board`:
```bash
python -m booley.ticket_board <subcommand>
```

---

## Modular Step Instructions

Each step's detailed instructions live in a separate file under `steps/`. **Before executing a step, read its file:**

```
steps/01-board-orphans.md   # Step 1: Show Board & Handle Orphans
steps/02-blocked.md         # Step 2: Blocked Tickets
steps/03-review.md          # Step 3: Review Tickets (health checks, approve/reject)
steps/04-summary.md         # Step 4: Triage Summary
```

For each step, read ONLY that step's file before executing it. Do NOT preload all steps.

---

## Conventions

- Process tickets in order: blocked → review
- "Failed" tickets are not a separate board state — `fail` is an alias for `block`, so failed tickets live in `blocked/` and are handled by the blocked step
- Within each category, process **oldest `last_update` first** (tickets waiting longest get attention first)
- After handling orphans, if no blocked/failed/review tickets remain → print "All clear" and STOP
- User can say "skip" on any ticket to leave it unchanged
- When diagnosis confirms a Booley harness, Flow, Specialist, or workflow bug or a Booley docs
  contradiction, invoke `/booley-feedback` by default after handling the
  ticket's immediate unblock/review decision. Do not merely bury it in the
  incident log or ask whether the user knows how to report it. The feedback
  skill verifies and captures the finding locally, then owns the separate
  preview and explicit approval gate for any external submission. Project RTL,
  ticket, configuration, and environment defects do not take this route.
- For review tickets with submodule changes (submodules listed in project/booley.toml or .gitmodules), always show the submodule diff
- Review tickets use the fixed briefing emitted by `booley board review-briefing`.
  Blocked tickets first use `booley board blocked-briefing`. Both commands are
  freshness-checking, read-only fast paths and never invoke an agent.
- The only review reports are the developer's `REPORT.md` and the rich HTML
  explanation. Do not generate `TRIAGE.md`, `run-summary.md`, or `usage.md`.
