# Booley Docs Principles

Rules for writing, editing, and reviewing Booley's documentation. They apply
to every doc. User docs, the docs written for the engineer who uses Booley
to get RTL work done, add the rules in
[USER_DOCS_PRINCIPLES.md](USER_DOCS_PRINCIPLES.md). That user talks to a
Developer Agent; the agent reads its skills, and contributors read
`docs/internals/`.

**Audience:** this document is written mainly for agents. Apply it when
writing, editing, or reviewing a doc. Human contributors follow the same
rules. It is to docs what [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md) is to
Python code, and review checks it the same way: cite the principle number.

**Scope:**

- **Every doc:** any `*.md` file and anything under `docs/`, except
  `CHANGELOG.md` and the fragments under `changelog.d/` that become its text
  (`changelog.d/README.md` stays in scope). ADRs also follow
  [docs/adr/README.md](../adr/README.md). Agent skills also follow
  `writing-for-agents` conventions where those are stricter.
- **User docs and the cheatsheet** also follow
  [USER_DOCS_PRINCIPLES.md](USER_DOCS_PRINCIPLES.md); its Scope lists
  their files. The cheatsheet follows
  [principle 33](USER_DOCS_PRINCIPLES.md#33-write-the-cheatsheet-for-the-user-and-the-agent)
  in that file.

In these principles, "the reader" is whoever the doc is for: the user in a
user doc, a contributor or agent in an internal doc.

---

## Process

### 1. Scope the doc

Name its job and its reader in one sentence, and say both at the top of the
doc. Point beginners to the simpler doc: "This goes deep. For everyday use,
Usage is enough." Done when you can say what belongs in the doc and which
neighboring doc gets everything else.

### 2. Judge every line

Ask of each line: does the reader need this? Apply every rule that covers the
doc. Each line gets one verdict: keep, rewrite, move (name the target doc), or
cut. Done when every line has a verdict, not just the ones that look odd.

Ask it of whole sections too, and of text a review asks you to add. A caveat,
edge case, or extra context that a reviewer wants passes the same test as the
rest: in a user doc it must clear principles 17,
[20](USER_DOCS_PRINCIPLES.md#20-write-for-the-user-not-the-agent),
and [21](USER_DOCS_PRINCIPLES.md#21-describe-outcomes-not-internals),
or it goes elsewhere.

### 3. Verify facts against the source

Check every command, flag, option name, default, path, platform condition,
and behavior claim against the code or `--help`. A doc that lies is worse
than a missing one. Done when each claim is checked or flagged as unverified.

When a check turns up a product bug or odd behavior, file an issue. Don't
write around it, and don't change source code in a documentation-only change.

### 4. Re-read as a newcomer

Read the result top to bottom as someone new to it: a first-time user for a
user doc, a new contributor or agent for an internal doc. List what they
would trip on or still miss: common tasks with no mention, terms they can't
decode, a next step with no link. Fix those, or offer them to the maintainer
as proposals.

### 5. Generalize review comments

When the maintainer reviews a doc with you, take their comments one at a time.
Apply each comment, then look for the same pattern in the rest of the doc and
its siblings, and offer to fix those too.

## Structure

### 6. The opening paragraph says what it is, when to use it, and when not to

Example: "Lint suits checking style and common bugs before simulation; it does
not show that the design works. Run it before the first simulation."

### 7. Concept before syntax

Explain what a thing is and its main kinds before its naming rules.

### 8. One job per doc, one topic per section

Move off-topic material to the doc it belongs in and leave a link. A feature
doc has no troubleshooting section:
[principle 34](USER_DOCS_PRINCIPLES.md#34-a-troubleshooting-entry-is-the-last-resort)
says where a problem goes.
Merge sections that cover overlapping ground and fold a minor behavior into
its parent. A feature doc leaves shared UI to that UI's doc. Split a giant doc:
one page per Flow, one per topic. Headings match their scope: a rule for all
Flows doesn't sit under a heading about one Flow.

### 9. Each fact has one home

Link instead of repeating. When a fact changes, search every doc and skill for
it and fix each one.

## Language

### 10. Plain words

If you can't explain a sentence in plain words, cut it: if the author doesn't
understand it, neither will the reader. Define a term or acronym on first use,
or replace it. Phrases like "decision grill", "feasibility triage", "gate",
"ADE", and raw notations such as `test[+variant][@seed]` need plain words. In
an internal doc, a link to the term's entry in [GLOSSARY.md](../GLOSSARY.md)
also works.

### 11. Exact and current words

Not "the most appropriate target", but the actual rule for which target runs.
Use the current names of commands, flags, options, modules, and functions.
Examples follow current conventions, because readers copy them: an example
Target is `sim_core`, never bare `sim` (see `tests/docs/test_target_naming_docs.py`).

### 12. State conditions exactly

"Windows only", "after the first build the image takes 4–6 GB". Keep real
warnings, and say when they apply.

### 13. American spelling

"behavior", "color", "optimize".

## Honest Claims

### 14. Describe current behavior only

Remove statements that stopped being true. History goes in the changelog; an
ADR states the decision that holds today and why. Words like "no longer",
"legacy", "renamed from", or
"since v0.4" fail `tests/docs/test_current_version_docs.py`. History without
those words counts too: "it sets no delay target" describes the old version
when only the old version set one, and "re-running init replaces `auto`"
describes upgrading from an old default. Say what the thing does now.

A retired setting is named in two places only: the message that refuses it,
which says what to do ("delete the key"), and the changelog. A reference doc
such as `CONFIG.md` lists only the current keys, and
[TROUBLESHOOTING.md](../user/TROUBLESHOOTING.md) gets no entry for it
([principle
34](USER_DOCS_PRINCIPLES.md#34-a-troubleshooting-entry-is-the-last-resort)).

### 15. Credit sources and mark temporary designs

Name the project an idea or implementation comes from. Say when a design is
temporary and link the roadmap.

### 16. Explain why

Justify design choices in a sentence: "the testbench reviewer never sees the
RTL, so it judges the tests against the spec without bias". In an internal doc
this is the main job: the code already says what happens. In a user doc, the
why is what the user gains; the evidence behind a choice (measurements,
rejected alternatives) goes in its ADR or pull request.

### 17. Short

Cut rule blocks down to what the reader acts on and merge thin sections into
their natural parent. Keep table rows to one line. Wrap prose at 80 columns: the
`markdown.line-length` check fails a pull request that adds or changes a longer
line, except in fenced code, table rows, headings, YAML front matter and
generated blocks, on lines that are only a link, URL or link reference
definition, and where the only text past column 80 is one URL or code span. No
signposting ("this is covered under Notes below") and no sentence that carries
nothing.

## Mechanics

### 18. Edit the source, never a rendered copy

The build renders shipped docs into `src/booley/data/docs/`. Never edit a
rendered copy. Rerun the generator of any generated block you touched
(CLI-REFERENCE Specialists, ARCHITECTURE).

### 19. Renaming a heading breaks its anchor

Update every link to the old anchor, or keep the old heading when outside
links depend on it.
