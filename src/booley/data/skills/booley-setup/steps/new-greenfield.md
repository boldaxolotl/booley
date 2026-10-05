# Greenfield mode (`new`) — from-scratch scaffold setup

> Part of the `booley-setup` skill. This path runs when `$ARGUMENTS` is `new`
> (or `greenfield`): a project **born with Booley** (`booley init --scaffold`),
> not a port of existing RTL. It replaces the plan phase (Steps 0–1) with a
> lightweight grill; the shared Steps 3–4 still run at the end.

Every flow is green by construction, including conventional simulation and lint
fail-path fixtures that let deep Doctor prove known-bad inputs fail, so there is
**no feasibility triage**. But
the choices still get made deliberately: run a **lightweight, dependency-aware
grill** in the **onboarding voice** (SKILL.md). Map the choices below as a
decision tree. Ask the whole current frontier — every unresolved choice whose
prerequisites are settled — in one round, then wait for the user's answers.
Because a scaffold starts unconstrained, the entire initial frontier normally
fits in one batched message rather than a long session. If an answer exposes a
dependent choice or contradicts another answer, recompute the frontier and ask
only the newly unblocked questions in the next round.

Assume the user is new to Booley, so each question carries a recommended answer
and a plain-English reason. Use the same format for every question:

```md
❓ **Q1** - **<question title>**: <question body, including choices when useful>

➡️ <recommended answer and why>
```

The grill covers:

- which flows to enable (ASIC synthesis? FPGA, and the part?);
- simulator (Verilator / Icarus) and testbench style (SystemVerilog / cocotb);
- lint EDA tool (Verilator / Verible);
- whether they want `AGENTS.md`;
- one merged git-history question (rows 16 + 20), asked exactly:
  **"Keep Booley out of your git history?"** Recommend No/open for a repo born
  with Booley: commit `.booley_project/`, use native cores, row 20 =
  `enabled = false`. Explain that Yes/hidden keeps `.booley_project/` untracked
  and enables stealth mode (`enabled = true`), including the commit-message
  scrub and hidden-core projection when Booley authors cores. Record both rows
  `user-confirmed` from this single answer; preserve hand-set `[stealth]` as
  `pre-set`. Use `../GLOSSARY.md` definitions verbatim on first use. Follow
  Step 0 row 16 for volunteered scrub exceptions, explicit hybrid requests,
  and the dependent ignore-native-cores question; unattended setup uses
  Step 0's unchanged fallback and review stars.

When the frontier is empty, summarize the shared understanding and ask the user
to confirm it. Do not write config or scaffold the project before confirmation.
Then record the answers in a minimal `SETUP-PLAN.md` (decision sheet + approval
only):

- **Repo not yet scaffolded** (no `.core`, no RTL): run
  `booley init --scaffold <name>` on the host with the flags matching the
  answers (`--sim-eda-tool`, `--tb-style`, `--lint-eda-tool`, `--asic`/`--no-asic`,
  `--fpga-part`).
- **Already scaffolded** (populated `.booley_project/` and a `.core`): don't
  re-ask what the scaffold already fixed — read the choices from the config,
  confirm them in the grill message, and record them.
- **Repo has existing RTL/`.core` but isn't scaffolded**: it's a port — run
  the normal plan phase instead of guessing.

Then **Reopen in Container** and run **Step 3 (optional — offer it) and Step 4
(the doctor gate)**. Use plain `booley doctor` while fixing the scaffold, then
follow Step 4's single final deep gate over the settled files; this summary does
not schedule an additional run. Do **not** declare the project ready until both
plain and deep Doctor exit 0 with zero active warnings.
For a scaffolded project
the sim/lint/synth smokes should pass before a line of design is written; that
green gate is the whole point of the mode. Confirm deep Doctor ran both the good
and bad sim/lint cases. Keep the generated Doctor-only fixtures separate from
the public design Targets; do not remove or defer them while presenting the
scaffold as Doctor-clean.
