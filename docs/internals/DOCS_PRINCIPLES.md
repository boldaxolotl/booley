# Booley Docs Principles

Rules for writing, editing, and reviewing Booley's documentation. Part 1
applies to every doc. Part 2 adds rules for user docs: the docs written for
the **driver**, the engineer who uses Booley to get RTL work done. The driver
talks to a Developer Agent; the agent reads its skills, and contributors read
`docs/internals/`.

**Audience:** this document is written mainly for agents. Apply it when
writing, editing, or reviewing a doc. Human contributors follow the same
rules. It is to docs what [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md) is to
Python code, and review checks it the same way: cite the principle number.

**Scope:**

- **Part 1, every doc:** any `*.md` file and anything under `docs/`, except
  `CHANGELOG.md`. ADRs record a decision as it stood when it was made, so
  principles 11 and 25 do not apply to them. Agent skills also follow
  `writing-for-agents` conventions where those are stricter.
- **Part 2, user docs:** `README.md`, `docs/user/`, `website/`, the bwave docs
  under `crates/bwave/docs/public/`, and the packaged copies under
  `src/booley/data/docs/` except `FLOW_REPORTS.md`, which is a contributor doc.

The reader in Part 1 is whoever the doc is for: the driver in a user doc, a
contributor or agent in an internal doc.

---

# Part 1: Every Doc

## Process

### 1. Scope the doc

Name its job in one sentence and who it is for. Done when you can say what
belongs in it and which neighbouring doc gets everything else.

### 2. Judge every line

Ask of each line: does the reader need this? Apply every rule that covers the
doc. Each line gets one verdict: keep, rewrite, move (name the target doc), or
cut. Done when every line has a verdict, not just the ones that look odd.

### 3. Verify facts against the source

Check every command, flag, option name, default, path, platform condition,
and behaviour claim against the code or `--help`. A doc that lies is worse
than a missing one. Done when each claim is checked or flagged as unverified.

### 4. Re-read as a newcomer

Read the result top to bottom as someone new to it: a first-time driver for a
user doc, a new contributor or agent for an internal doc. List what they
would trip on or still miss: common tasks with no mention, terms they can't
decode, a next step with no link. Fix those, or offer them to the maintainer
as proposals.

### 5. Generalise review comments

When the maintainer reviews a doc with you, take their comments one at a time.
Apply each comment, then look for the same pattern in the rest of the doc and
its siblings, and offer to fix those too.

## Order

### 6. The opening paragraph says what it is, when to use it, and when not to

Example: "Goal Mode suits area/timing optimisation and new features where the
design must stay correct; it is not built for debugging. It keeps long
unattended runs within budget."

### 7. Concept before syntax

Explain what a thing is and its main kinds before its naming rules.

## Plain Language

### 8. If you can't explain a sentence in plain words, cut it

If the author doesn't understand it, neither will the reader.

### 9. Define a term on first use, or replace it

Phrases like "decision grill", "feasibility triage", "gate", and raw notations
such as `test[+variant][@seed]` need plain words. In an internal doc, a link
to the term's entry in [GLOSSARY.md](../GLOSSARY.md) also works.

### 10. Use exact words

Not "the most appropriate target", but the actual rule for which target runs.

## Honest Claims

### 11. Keep claims current

Remove statements that stopped being true.

### 12. Credit sources

Name the project an idea or implementation comes from.

### 13. Mark temporary designs

Say a design is temporary and link the roadmap.

## Every Section Earns Its Place

### 14. One topic, one section

Merge sections that cover overlapping ground; fold a minor behaviour into its
parent section.

### 15. One job per doc

Move off-topic material to the doc it belongs in and leave a link.
Troubleshooting stays in [TROUBLESHOOTING.md](../user/TROUBLESHOOTING.md).

## Explain Why

### 16. Justify design choices

In a sentence: "the testbench reviewer never sees the RTL, so it judges the
tests against the spec without bias". In an internal doc this is the main
job: the code already says what happens.

## Short

### 17. Cut rule blocks down to what the reader acts on

Merge thin sections into their natural parent.

### 18. Keep table rows to one line

### 19. No signposting and no empty sentences

No "this is covered under Notes below", and no sentence that carries nothing.

## Precise

### 20. State conditions exactly

"Windows only", "after the first build the image takes 4–6 GB". Keep real
warnings, and say when they apply.

### 21. Use current names

For commands, flags, options, modules, and functions.

### 22. Headings match their scope

A rule that applies to all Goals doesn't sit under a heading about one kind of
Goal.

## Doc Set

### 23. Split giant docs

One page per Flow, one per topic.

### 24. Say up front who the doc is for

Point beginners to the simpler doc: "This goes deep. For everyday use, Usage is
enough."

### 25. Describe current behaviour only

Migration notes and "now does X" go in the changelog; decision history goes in
ADRs.

### 26. Link instead of repeating

Each fact has one home.

---

# Part 2: User Docs

Every line in a user doc is written for the driver.

## Process

### 27. Preview the website

For a page under `website/`, build it locally (`pip install -r
website/requirements.txt`, then `mkdocs serve -f website/mkdocs.yml`) and give
the maintainer the URL to review the rendered page.

## Audience

### 28. Agent-facing content goes to the agent's skill

Internal tool and API names, parameter-passing rules, file syntax the agent
writes, and instructions like "never weaken tests" belong in the skill under
`src/booley/data/skills/`, not the user doc. In the user doc, point the driver
to what they see (the Dashboard, a report) instead.

### 29. The driver asks the agent

Write "ask the agent to create a worktree named `zbb`" instead of a CLI
command. Exception: docs about CLI commands themselves.

### 30. Document what drivers actually do

Leave out commands nobody runs by hand and statements of things a human can't
do anyway ("you never call the MCP tools yourself").

## Implementation Detail

### 31. Describe outcomes, leave out internals

Registration mechanics, reapers, heartbeats, caches, what CI does, and the
full list of steps a command performs belong in internal docs. Keep one line
on what the driver gets out of it.

### 32. Leave out special cases the driver never meets

For example, behaviour inside Booley's own source checkout.

## Order

### 33. How-to follows the opening, as a numbered list of the real steps

Include the points where the human has to act ("the agent stops and confirms
the Goals with you").

### 34. Reference goes last and stays short

Syntax, file formats, presets. The less the driver writes it by hand, the
shorter it gets.

## Plain Language

### 35. Use the driver's words

"Run a test N times with different seeds, or pin one seed" instead of the
internal model.

## Honest Claims

### 36. State the problem at its real size

No exaggerated pain points and no overpromising.

### 37. Leave unreleased features out

## Every Section Earns Its Place

### 38. List only real features

The default way of working is not a feature.

### 39. A feature doc covers only what is specific to that feature

Leave generic descriptions of shared UI to that UI's doc.

## Explain Why

### 40. List the advantages that set Booley apart

Properties that plain alternatives also have don't sell anything.

### 41. Make each benefit concrete

If a benefit takes effort to understand, rewrite it with a concrete case.

### 42. Describe what a thing does, not what it is built from

"Checks style and common RTL mistakes", not "Verilator and Verible lint".

## Short

### 43. Example prompts are short and natural

Leave out boilerplate instructions the agent follows anyway ("don't edit
files"), and don't describe what a good answer looks like.

## Precise

### 44. Mirror parallel variants

If one step shows the Codex form next to the Claude form, every step does.

## Doc Set

### 45. Order docs along the driver's journey

Features, install, setup, usage, then deep dives.

### 46. End with a way out

How to report a bug, with an email and an issue link.
