# Greenfield mode (`new`) — from-scratch scaffold setup

> Part of the `booley-setup` skill. This path runs when `$ARGUMENTS` is `new`
> (or `greenfield`): a project **born with Booley** (`booley init --scaffold`),
> not a port of existing RTL. It replaces the plan phase (Steps 0–1) with a
> lightweight grill; the shared Steps 3–4 still run at the end.

Every flow is green by construction, so there is **no feasibility triage**. But
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
- the git footprint (row 16) — a repo born with Booley usually wants
  `.booley_project/` committed, but ask rather than assume;
- stealth mode (row 20) — its hidden-core projection is required only for a
  hidden authored core, while an open scaffold may still opt into its history
  scrub. In the same batched message ask exactly: **"Do you want stealth mode:
  self-contained hidden cores plus the commit-message scrub?"** Recommend
  disabled for an open scaffold and enable it only from an explicit yes.

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
(the doctor gate)**: `booley doctor`, fix, `booley doctor --deep`, fix, and do
**not** declare the project ready until both exit 0 with zero active warnings.
For a scaffolded project
the sim/lint/synth smokes should pass before a line of design is written; that
green gate is the whole point of the mode.
