# Greenfield mode (`new`) — from-scratch scaffold setup

> Part of the `booley-setup` skill. This path runs when `$ARGUMENTS` is `new`
> (or `greenfield`): a project **born with Booley** (`booley init --scaffold`),
> not a port of existing RTL. It replaces the plan phase (Steps 0–1) with a
> lightweight grill; the shared Steps 3–4 and report-writing Step 6 run at the end.

In the initial scaffold, every flow is green by construction, including
conventional simulation and lint fail-path fixtures that let deep Doctor prove known-bad inputs fail, so there is
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
- when ASIC synthesis is enabled, synthesis mode: `physical` (recommended default,
  Yosys + OpenROAD with constrained timing and SDC) or `logical` (Yosys mapped
  area only, no SDC). Ask this dependent choice after ASIC enablement is settled;
- simulator (Verilator / Icarus) and testbench style (SystemVerilog / cocotb);
- lint EDA tool (Verilator / Verible);
- whether they want `AGENTS.md`;
- one merged git-history question (rows 16 + 20), asked exactly:
  **"Keep Booley out of your git history?"** Recommend No/open for a repo born
  with Booley: commit `.booley/`, use native cores, row 20 =
  `enabled = false`. Explain that Yes/hidden keeps `.booley/` untracked
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
  `--synth-mode logical|physical`, `--fpga-part`). Omit `--synth-mode` when
  ASIC is disabled: an explicit mode with `--no-asic` is rejected.
- **Already scaffolded** (populated `.booley/` and a `.core`): don't
  re-ask what the scaffold already fixed — read the choices from the config,
  confirm them in the grill message as `pre-set`, and record them in SETUP-PLAN.
  Read `flow_options.synth_mode`; missing mode means the physical default.
  A request that differs from existing choices needs the existing plan/confirmation
  and Target-edit process: record the deviation, never destructively re-scaffold.
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

When replacing the scaffold RTL/interface or testbench, refresh the simulation
bad overlay under the resolved Booley Directory's `selftest/sim/bad-overlay/`
to match the current interface while retaining one deliberate behavioral defect
that builds and fails the selected smoke Test.

## Additional recipe requests

The scaffold is a runnable counter starter. Record each extra request in SETUP-PLAN with what will be applied or why it is excluded; obtain existing setup confirmation for each deviation.

- PPA: keep the `balanced` starter with no mandatory question/profile flag; apply volunteered `compact` or `max_frequency` through existing Target configuration/full setup and report a deviation until applied.
- `frontend` and `flatten`: keep defaults; confirm expert changes in the existing Target recipe through full setup.
- Backend expert settings, technology/physical-library choices and custom EDA tools: require design/environment evidence and full setup provisioning; record unsupported requests and claim only tools/libraries actually used. Logical synthesis cannot consume OpenROAD settings.
- Starter constraints: physical uses a 10 ns SDC, logical emits no SDC, FPGA XDC is independent; design-specific clocks, board pins, sources, interfaces and parameters follow Step 0/2 authority, never invented real-project constraints.
- Sandbox images, provider/auth and EDA provisioning: use existing initialization/full setup and record unmet requests.
- Project-specific verification intent (tests, seeds, variants, tracing, coverage, timeout, resources): use full setup; the starter's two deterministic Tests and known-bad Doctor fixtures do not prove unexecuted requested checks.
- IP name, simulator, TB style, linter, ASIC/FPGA enablement and part, optional AGENTS.md and history/stealth: use the existing choices above.

After Doctor, run **Step 6 §2 (write the report), §3 (walk through it), and §5
(close out)** per the shared **Delivered Flow results** contract in SKILL.md;
triage and export keep Step 6's existing consent requirements. Then run **Step 7**
for cleanup, removing the run's scratch root. The final
summary reuses the report's evidence rows, including unverified checks and
deviations.
