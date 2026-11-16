# Booley User Docs Principles

Rules for writing, editing, and reviewing Booley's user docs: the docs written
for the engineer who uses Booley to get RTL work done. They add to
[DOCS_PRINCIPLES.md](DOCS_PRINCIPLES.md), which still applies to every doc and
holds principles 1 to 19. The numbering continues here, so a principle number
names one rule in either file. Read both before you write, edit, or review a
user doc or the cheatsheet; any other doc follows DOCS_PRINCIPLES.md alone.

**Audience:** this document is written mainly for agents. Human contributors
follow the same rules, and review cites the principle number.

**Scope:**

- **User docs:** `README.md`, `docs/user/`, `website/`, and the bwave docs
  under `crates/bwave/docs/public/`.
- **The cheatsheet:** `src/booley/data/cheatsheet.md`, printed by
  `booley cheat`, follows principle 33 of this file and every principle of
  DOCS_PRINCIPLES.md.

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
The cheatsheet is the exception: the agent reads it too (principle 33).

### 21. Describe outcomes, not internals

Registration mechanics, reapers, heartbeats, caches, what CI does, and the
full list of steps a command performs belong in internal docs; keep one line
on what the user gets out of it. Leave out special cases the user never meets,
such as behavior inside Booley's own source checkout.

Keep what the user needs to trust and read the result: what a Flow changes in
their design, what it checks, and how it picks. A Mutation Flow doc names the
kinds of Mutants it plants and how it chooses them.

- **No measurements from one design.** "On picorv32 at 0.8 ns it shortens the
  critical path by 2.3%" belongs in the pull request or ADR. State the
  trade-off in general words: "a shorter critical path for a little more area
  and a longer run".
- **No tours of a UI the user can see.** A Dashboard form or wizard gets one
  line: where to find it and what it gives. Its fields explain themselves.

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
stops and confirms the Target with you"). Reference goes last and stays short:
syntax, file formats, presets. The less the user writes it by hand, the
shorter it gets.

### 25. One recommended route

When several routes reach the same result, document the recommended one and
drop the rest.

### 26. Variants go in collapsed callouts

The main text describes one baseline setup: Stealth Mode off, an HDL
testbench, Linux, the EDA tools in the Sandbox Image, and the open 45 nm cell
library. A user on that setup never has to skip a sentence. When a variant
changes what the user sees or does, put the difference in a collapsed callout
right after the baseline text it modifies:

```markdown
<details markdown>
<summary>In stealth mode</summary>

Commit `AGENTS.md` in the Booley Directory's own repository instead:
`git -C .booley commit`.

</details>
```

- Use one fixed label per variant: `In stealth mode`, `With a cocotb
  testbench`, `On Windows`, `With host-installed licensed tools`, `With your own
  image`, and `With the RISC-V image`.
- Say only what differs, and link the variant's own page or section
  (`STEALTH.md`, `FEATURES.md#windows-support`, ...) for the full story. Keep
  the callout at block level, not inside a list item, and leave blank lines
  around its Markdown body.
- Leave in the main text: choices every user makes (port or scaffold, FPGA or
  ASIC), hard limits (no coverage on Icarus), and reference tables that must
  list every value.

**EDA tools are variants too.** A Flow doc (a `FLOW-*.md` page, or a guide
for one Flow such as `TESTS.md` and `COVERAGE.md`) describes what the Flow
does in words that hold for every EDA tool it supports, and so does any
summary of a Flow in another user doc. A Flow promises the same call and
result whichever tool runs it, and tool-neutral text stays true when a tool
is added.

- Collapse only where tools differ. A fact every supported tool shares is
  written neutrally and stays in the main text; a Flow with one tool keeps
  its content visible.
- When a tool changes what the user sees or does, put the difference in a
  `With <Tool>` callout (`With Verilator`, `With Xcelium`).
- Tool setup, such as waiver files and rule configs, goes in that tool's
  Target recipe in the Flow doc. `TARGETS.md` covers only what every Flow's
  Targets share.
- Hard limits stay in the main text. So does example output, labeled with
  the tool that produced it: "Example (Verilator):".

**Cell libraries are variants too.** ASIC synthesis maps to one cell library
today, but the same rule keeps its docs true when a PDK is added. Say "the
cell library" and name the 45 nm library only where the reader needs its
value, such as a reference table. When a library changes what the user sees
or does, put the difference in a `With the <name> library` callout.

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

## The Cheatsheet

### 33. Write the cheatsheet for the user and the agent

You read `booley cheat` at a terminal, and the Developer Agent reads it to
look up commands, Goal syntax, and the rules it works by. Every entry must
make sense to both readers:

- **"You" is the person.** Name the agent by role, "the Developer Agent",
  for what it does or must not do. Never write to "the agent" and never write
  about "the user" in the third person. Then "only you approve" can't be read
  by the agent as permission for itself.
- **Every command works as written** for a person typing it and for the agent
  reading it. Give the CLI form beside a tool-call argument
  (`verify=true`, CLI: `--verify`).
- **Gloss agent-only terms once** in the section that uses them: say that a
  `goal_*` name is a Developer Agent tool, not a command, and what `MCP` and
  `work_dir` mean.

Bad: "Measure only when the user asks for a Measured Commit." Good: "The
Developer Agent measures only when you ask for a Measured Commit."

## Troubleshooting

### 34. A troubleshooting entry is the last resort

[TROUBLESHOOTING.md](../user/TROUBLESHOOTING.md) holds only the problems
Booley can't explain to the user by itself. Before you add an entry, or accept
one in review, go down this list and stop at the first line that fits:

1. **Booley can detect it:** add a Doctor check whose hint is the fix. No
   entry titled "Doctor reports X".
2. **Booley prints a message about it:** put the fix in the message. An entry
   that explains an error message is a bug report on that message.
3. **It fails silently or hangs:** that is a product bug.
4. **A release caused it** (a retired key, an upgrade step, leftovers of an
   older version): the changelog and the refusal message cover it
   (principle 14).
5. **The average user meets it** in normal use of a feature: it belongs in
   that feature's doc, as how the feature behaves.
6. **Another tool's own error** (Verilator, GCC, Git, Docker, pip), which that
   tool's message and docs resolve: no entry. The exception is a tool whose
   usual fix fails in the Sandbox, or whose error names the wrong thing.

For lines 1 to 3, file an issue instead of writing the entry (principle 3),
and put the workaround in the issue.

What is left gets an entry: a rare situation whose symptom doesn't name its
cause and that Booley can't detect.

- **The heading is the symptom** as the user meets it, in the words of the
  error or of what they see: "RTL opens as plain text", not "Coverage Campaign
  diagnostics".
- **The body is the cause in one sentence, then the fix.** No reference
  tables, and no "never delete X" rule without a symptom.

Bad: an entry "Missing Seat Cap" that repeats the fix the refusal already
prints. Good: no entry, because the message says "add `[jobs.seats]
vivado = <seats you own>` to `booley.toml`".
