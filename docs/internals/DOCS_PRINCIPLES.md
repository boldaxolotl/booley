# Booley Docs Principles

Rules for writing, editing, and reviewing Booley's documentation. Part 1
applies to every doc. Part 2 adds rules for user docs: the docs written for
the engineer who uses Booley to get RTL work done. That user talks to a
Developer Agent; the agent reads its skills, and contributors read
`docs/internals/`.

**Audience:** this document is written mainly for agents. Apply it when
writing, editing, or reviewing a doc. Human contributors follow the same
rules. It is to docs what [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md) is to
Python code, and review checks it the same way: cite the principle number.

**Scope:**

- **Part 1, every doc:** any `*.md` file and anything under `docs/`, except
  `CHANGELOG.md` and the fragments under `changelog.d/` that become its text
  (`changelog.d/README.md` stays in scope). ADRs record a decision as it stood
  when it was made, so principle 14 does not apply to them. Agent skills also follow
  `writing-for-agents` conventions where those are stricter.
- **Part 2, user docs:** `README.md`, `docs/user/`, `website/`, and the bwave
  docs under `crates/bwave/docs/public/`.

In Part 1, "the reader" is whoever the doc is for: the user in a user doc, a
contributor or agent in an internal doc.

---

# Part 1: Every Doc

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

Example: "Goal Mode suits area/timing optimization and new features where the
design must stay correct; it is not built for debugging. It keeps long
unattended runs within budget."

### 7. Concept before syntax

Explain what a thing is and its main kinds before its naming rules.

### 8. One job per doc, one topic per section

Move off-topic material to the doc it belongs in and leave a link;
troubleshooting stays in [TROUBLESHOOTING.md](../user/TROUBLESHOOTING.md).
Merge sections that cover overlapping ground and fold a minor behavior into
its parent. A feature doc leaves shared UI to that UI's doc. Split a giant doc:
one page per Flow, one per topic. Headings match their scope: a rule for all
Goals doesn't sit under a heading about one kind of Goal.

### 9. Each fact has one home

Link instead of repeating. When a fact changes, search every doc and skill for it and fix each one.

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

Remove statements that stopped being true. History goes in the changelog and
decision history in ADRs. Words like "no longer", "legacy", "renamed from", or
"since v0.4" fail `tests/docs/test_current_version_docs.py`. Write a retired
setting as an action: "Delete `[flows.sim].standalone_frontend` if your
`booley.toml` sets it."

### 15. Credit sources and mark temporary designs

Name the project an idea or implementation comes from. Say when a design is
temporary and link the roadmap.

### 16. Explain why

Justify design choices in a sentence: "the testbench reviewer never sees the
RTL, so it judges the tests against the spec without bias". In an internal doc
this is the main job: the code already says what happens.

### 17. Short

Cut rule blocks down to what the reader acts on and merge thin sections into
their natural parent. Keep table rows to one line. No signposting ("this is
covered under Notes below") and no sentence that carries nothing.

## Mechanics

### 18. Edit the source, then regenerate copies

The build renders shipped docs into `src/booley/data/docs/`. Never edit a
rendered copy. Rerun the generator of any generated block you touched
(CLI-REFERENCE Specialists, ARCHITECTURE).

### 19. Renaming a heading breaks its anchor

Update every link to the old anchor, or keep the old heading when outside
links depend on it.

---

# Part 2: User Docs

Every line in a user doc is written for the user.

## Audience

### 20. Write for the user, not the agent

Internal tool and API names, parameter-passing rules, file syntax the agent
writes, and instructions like "never weaken tests" belong in the skill under
`src/booley/data/skills/`. In the user doc, point to what the user sees (the
Dashboard, a report) instead. The user asks the agent: write "ask the agent
to create a worktree named `zbb`" instead of a CLI command, except in docs
about CLI commands themselves. Leave out commands nobody runs by hand and
things a human can't do anyway ("you never call the MCP tools yourself").

### 21. Describe outcomes, not internals

Registration mechanics, reapers, heartbeats, caches, what CI does, and the
full list of steps a command performs belong in internal docs; keep one line
on what the user gets out of it. Leave out special cases the user never meets,
such as behavior inside Booley's own source checkout.

### 22. User pages stand alone

Never send the user to a contributor doc such as `FLOW_REPORTS.md`; only
contributor docs published on the website (ARCHITECTURE) may be linked. Show
the real end-of-run output and the few files a user opens; file formats
belong in the contributor doc.

### 23. Use the user's words

"Run a test N times with different seeds, or pin one seed" instead of the
internal model.

## Structure

### 24. What, why, when; then how; then reference

After the opening paragraph (principle 6), the how-to comes as a numbered list
of the real steps, including the points where the user has to act ("the agent
stops and confirms the Goals with you"). Reference goes last and stays short:
syntax, file formats, presets. The less the user writes it by hand, the
shorter it gets.

### 25. One recommended route

When several routes reach the same result, document the recommended one and
drop the rest.

### 26. Variants go in collapsed callouts

The main text describes one baseline setup: Stealth Mode off, an HDL
testbench, Linux, and the EDA tools in the Sandbox Image. A user on that setup
never has to skip a sentence. When a variant changes what the user sees or
does, put the difference in a collapsed callout right after the baseline text
it modifies:

```markdown
<details markdown>
<summary>In stealth mode</summary>

Commit `AGENTS.md` in the Booley Directory's own repository instead:
`git -C .booley commit`.

</details>
```

- Use one fixed label per variant: `In stealth mode`, `With a cocotb
  testbench`, `On Windows`, `With host-installed licensed tools`.
- Say only what differs, and link the variant's own page (`STEALTH.md`,
  `WINDOWS.md`, ...) for the full story. Keep the callout at block level, not
  inside a list item, and leave blank lines around its Markdown body.
- Leave in the main text: choices every user makes (port or scaffold, FPGA or
  ASIC), hard limits (no coverage on Icarus), and reference tables that must
  list every value.

### 27. Mirror parallel variants

If one step shows the Codex form next to the Claude form, every step does.

## Honest Claims

### 28. Claim only what is real and released

State the problem at its real size: no exaggerated pain points and no
overpromising. Leave unreleased features out. List only real features: the
default way of working is not a feature.

### 29. Sell with specifics

List the advantages that set Booley apart; properties that plain alternatives
also have don't sell anything. Make each benefit concrete: if it takes effort
to understand, rewrite it with a concrete case. Describe what a thing does,
not what it is built from: "checks style and common RTL mistakes", not
"Verilator and Verible lint".

### 30. Example prompts are short and natural

Leave out boilerplate instructions the agent follows anyway ("don't edit
files"), and don't describe what a good answer looks like.

## Doc Set and Rendering

### 31. Order docs along the user's journey, and end with a way out

Features, install, setup, usage, then deep dives. End with how to report a
bug, with an email and an issue link.

### 32. Render the same on GitHub and the website

Indent content inside a list item by 4 spaces (`tests/docs/test_user_docs.py`)
and use relative repository links. For a page under `website/`, build it
locally (`pip install -r website/requirements.txt`, then `mkdocs serve -f
website/mkdocs.yml`) and give the maintainer the URL to review the rendered
page.
