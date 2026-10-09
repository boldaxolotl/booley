# Step 0 — Plan (feasibility + decision grill → SETUP-PLAN.md)

> Part of the `booley-setup` skill. This step runs on the **host**, before the
> devcontainer exists. It is **read-only with respect to the repository** — its
> only write is `.booley_project/SETUP-PLAN.md` (the state dir; `booley init`
> created it). No project config is authored here; that is Steps 2–4, and they
> run only against an approved plan.

Every decision the later steps need is gathered, decided, and approved **here,
up front** — feasibility triage plus a full decision sheet, refined through a
grilling session with the user. Execution (Steps 1–4) then runs gate-free: the
steps consume the plan instead of stopping to ask, deviating only per the
deviation rule in `SKILL.md`.

The step has four parts: **A** — gather feasibility evidence; **B** — turn it
into a decision sheet; **C** — grill the user on the open rows; **D** — write
the plan and get it approved.

Use `../GLOSSARY.md` definitions verbatim on first use of each Booley term in
the grill or plan; do not paraphrase from `GLOSSARY.md`. Ask each question by its
plain label from the template; give the row number/internal key only in
parentheses.

## Interactive vs. unattended

- **Interactive (default).** A human is present. Run the grill in Part C and
  the approval gate in Part D. This is the only stop-and-wait gate in the whole
  skill — everything after an approved plan executes without interruption.
- **Unattended.** The agent was handed an explicit, pre-approved end-to-end
  setup request and nobody will answer questions. No question is asked, in
  either mode of the word: resolve every row by the rule below, set the plan
  status to `auto-approved`, and proceed to execution. The plan document is the
  audit trail the user reads afterward, so its Resolution column has to say
  *how* each row was settled and its Confidence column how sure you are. Every
  row the user must audit is starred `review`; nothing else is. The deviation
  rule still holds — a
  plan-invalidating contradiction during execution halts and records the open
  question; it never improvises a new plan.

### How a row resolves

**Two separate columns, never merged.** *How* a row was settled and *how sure*
you are are different facts, so the sheet carries both:

- **Resolution** — the mode the row was settled in (below). One value.
- **Confidence** — `high`/`medium`/`low`, the strength of the inference behind
  the value. Write `—` where resolution leaves nothing to infer
  (`evidence-forced`, `user-confirmed`, `pre-set`).

Squashing them ("high ×4 flows + review ×1" in one cell) makes the sheet
unreadable and hides which rows the user must audit.

**A row that covers several independent items resolves per item.** Row 1 (four
flows) is the usual offender: either split it into one sub-row per flow, or
write the mode per item (`sim/lint/synth: evidence-forced · fpga: review`). A
single cell must never average two different modes.

The resolution modes:

- **`evidence-forced`** — the repo determines the value uniquely and no other
  answer is defensible: 100% of the tests are cocotb ⇒ row 4 is `cocotb`; no
  native-flow EDA tool matches Booley's ⇒ row 18 is `none`. **Auto-approved with
  its evidence recorded, in both modes** — do not star it, do not ask it. In
  interactive mode, state it in the grill as a fact, not a question.
- **`pre-set`** — the value was already on disk when Step 0 started (a hand-set
  knob, an earlier setup's config). Keep it verbatim, evidence = the config
  line; not starred, not re-asked, in either mode. See the prior-footprint
  branch below.
- **`user-confirmed`** — the user answered it in the grill (interactive only).
- **`inferred`** — an ordinary reading of the repo; carry a `high`/`medium`/
  `low` confidence beside it. `low` becomes `review` in unattended mode.
- **`review`** — a genuine user judgment call, or an inference the evidence
  points at but does not force. **Rows 16 (git footprint), 19 (agent backend),
  and 20 (commit-message scrub) can never be evidence-forced.** Interactive:
  ask the merged history question and any missing backend choice. Unattended:
  take the documented fallback, star the row `review`, and surface it in the
  final report as something the user must audit.

Row 17 has no codebase signal for an intentional Specialist opt-out: it takes
its default (none disabled) and is shown in the defaults block. Unattended:
record that default as `inferred`/high, without a star.

"Mandatory grill row" means row 4 (unless evidence-forced), merged rows 16 + 20,
and row 19 only when init left a field unset. Existing hand-set values resolve
`pre-set` and receive one confirmation line rather than a new question. In
unattended mode, unresolved mandatory choices take their documented fallback
and an explicit `review` star; evidence-forced and pre-set rows are unstarred.

## Part A — Feasibility triage (evidence)

Booley can drive four flows: **`sim`**, **`lint`**, **`synth`**,
and **`fpga`**. Feasibility is per-flow: a project may be a perfect fit
for simulate and lint while its synthesis flow needs extra wiring. Per flow the
verdict falls into one of three buckets. The authoritative EDA-tool → bucket mapping
lives in the supported EDA tools matrix, `docs/user/SUPPORTED-EDA-TOOLS.md` — treat it as the
source of truth; the EDA-tool names in the buckets below are illustrative and can
drift as the matrix grows. **Read the copy that matches the Booley you are
setting up** (`booley --version` names it): a local Booley *checkout* on this
host may be an unmerged dev tree that is ahead of — or diverged from — the
installed package, and a matrix row that only exists there is not a capability
this project has. Order of preference: the installed package's own docs, then a
Booley checkout **at the matching commit**, then GitHub at that tag. If they
disagree, the installed version wins; name in the plan which copy you read.

The buckets:

- **Green:** the built-in flow covers it as-is. SystemVerilog/Verilog RTL on
  Verilator or Icarus (sim and lint), or Yosys + OpenROAD (ASIC
  synthesis), or host-provisioned Vivado 2025.2 on Linux x86-64 when an
  administrator has already registered and granted the installation and no
  unvalidated floating-license behavior is required. Fastest path.
- **Yellow:** feasible, but something the plan **cannot fully settle through
  the approved Sandbox Policy** is still in the way. Three shapes: an external
  dependency or experimental gate (a missing Vivado registration/Grant, or a
  required floating FlexNet checkout whose real paid-site behavior has not been
  validated); an
  input the repo **does not ship and somebody must provide** (a flat-port
  wrapper or a pass/fail sentinel a directed TB never prints, which you author;
  or an SDC for a physical synth Target or an XDC for FPGA, which only the user
  may supply, as row 10 explains); or a mechanical conversion whose input you
  have not actually read yet (a `.fl` filelist, an EDA-tool-API `.core`).
- **Red:** out of reach today. A simulator outside the built-in matrix
  (Questa/ModelSim, VCS today), VHDL-only RTL against the built-in Verilog
  engines, encrypted RTL with no licensed simulator, or a license daemon that
  is unreachable for good (not just down for a restart). Widening the matrix
  is a Booley extension, not a project setup task.

**Calibrate on unresolved risk, not on config volume.** The verdict is what
warns the reader where this setup can still go wrong, so a step you have
*measured* stays Green no matter how much config it needs — a
`pre_run_commands` vector/firmware build with exact command lines and a timed
Sandbox run (e.g. "17 s, 3.3 GB, `tests/generate.sh`") is planned work,
not risk.

A flow that needs an artifact **nobody has written yet** is Yellow even when
everything else about it is ordinary. If a determinant made you write "someone
must author X" or "someone must confirm Y", that flow is Yellow; if every open
item is a command you already know and priced, it is Green.

A red flow doesn't sink the project: plan the flows that are green or yellow
and that the user actually wants. Only an all-flows red means Booley isn't a
fit for this IP yet.

### Prior Booley footprint (check this first)

Before gathering anything else, find out whether someone has already pointed
Booley at this repo — a second setup that assumes a blank slate silently
overwrites the first one's decisions.

**Know `booley init`'s own baseline first**, or every fresh repo reads as a
prior port. A plain `init` (which already ran — it is what deployed this skill)
leaves all of this on a blank slate:

- `.booley_project/booley.toml` and `tests.toml` — **comment-only placeholders
  with zero keys**;
- `.booley_project/.gitignore`, a `FUSESOC_IGNORE` marker, a managed
  `.managed/project-git-hooks.pyz` bundle, Project-authored `hooks/`, and four create-only
  `goalsets/{feature,bugfix,refactor,verification}.md` files;
- an **inner git repo** at `.booley_project/.git`, with no commit in it — but
  only when `[stealth] enabled` is on (the runtime fallback before setup makes
  its explicit choice); with the scrub explicitly off, init skips it and the
  dir is versioned nowhere;
- `.git/hooks/commit-msg` + `pre-push` delegators, and
  `.devcontainer/devcontainer.json`;
- one generated block in `.git/info/exclude` under the header
  `# Booley (generated; local, uncommitted)`: `/.devcontainer`,
  `/.booley_project`, `/.claude`, and `/.booley-projected-*.core`. Init consolidates repeated headers.

Tell-tales of a **prior setup** are therefore only things init never writes:
any actual key in `booley.toml`/`tests.toml` (a `[sandbox].image`, `[stealth]`,
`[flows]`, or `[agent]` block), a `.booley_project/docker/` or `cores/` dir, a
`.booley_project/AGENTS.md` (and its second exclude block,
`# Booley guidance links` → `/AGENTS.md`, `/CLAUDE.md`), an existing
`SETUP-PLAN.md`, `<axis>_<subject>` targets in a `.core`, or a **tracked**
`SETUP-REPORT.md` at the repo root. Then branch:

**The branches are not mutually exclusive — apply every one that matches.** A
repo routinely lands in two at once (a hand-set knob *and* a tracked artifact);
the only branch that short-circuits the rest is the first.

- **`SETUP-PLAN.md` exists** — this is not a fresh Step 0. Hand back to
  `SKILL.md`'s phase detection (`complete` ⇒ offer a single-step re-run;
  `executing` ⇒ resume). Stop here; the other branches don't apply.
- **Config but no plan** (a pre-plan-first or hand-made setup) — do not
  overwrite it. Load every hand-set knob into the decision sheet as the
  *decided* value with evidence "hand-set in `<file>:<line>`, pre-existing", and
  grill only what is missing or contradicted. `[sandbox].image` (row 7), the git
  footprint (row 16), and `[stealth]` (row 20) are decisions a human already
  made; never silently reset them.
  **How a hand-set knob resolves** — this is the one case where a
  never-evidence-forced row (16, 17, 19, 20) resolves without asking, because
  the value is not a codebase inference, it is *the user's own earlier answer*.
  Resolve it `pre-set`, value = exactly what is on disk, evidence = the config
  line. **Do not star it `review`** (the user already made this call) and do not
  star the fallback you would otherwise have used — the fallback is not in play.
  Interactive: state it as a confirmation line, don't re-grill it. The only
  thing that reopens a `pre-set` row is a direct contradiction (the hand-set
  image doesn't exist, the hand-set Target is gone) — and that is the deviation
  rule, not a silent reset.
- **Tracked Booley artifacts** (a committed `SETUP-REPORT.md`, a port's `.core`
  edits) — record them in row 16 as pre-existing tracked footprint and **leave
  them alone**. `SKILL.md`'s footprint guardrail forbids *adding* Booley-
  generated files to the tracked tree; it does not ask you to delete what an
  earlier run or the maintainer dogfood workflow already committed. Removing
  tracked files is the user's call, not a setup step.
  **A tracked `SETUP-REPORT.md` is not proof the port is finished.** Check its
  mtime and `git log -1 --format=%cr -- SETUP-REPORT.md`: on a maintainer
  dogfood repo it is usually the **current** port's report, created hours ago, with
  sections still waiting on the step you are about to run. Either way your
  handling is identical — read it as evidence (it names the repo's traps), never
  write to it from Step 0, and never treat its existence as "setup already
  happened". Only `SETUP-PLAN.md` decides the phase.
- **Placeholders only** — the normal path; carry on.

**Probing rules.** Everything in this step is host-side and read-only:
`rg --files` inventories, reading docs/CI/Makefiles/filelists,
`find . -type l`, counting `.core` files, license-daemon reachability. What
you may NOT do yet: run EDA tools in the sandbox, resolve targets through real
`fusesoc`, or write any file outside `.booley_project/SETUP-PLAN.md`. Where
only an in-sandbox probe would settle a question, record the decision at its
honest confidence and add the probe to the plan's **execution-time checks**
list — Steps 2–4 run it, and the deviation rule catches a contradiction.

### The determinants

Work through every determinant below. Each is both a feasibility input and a
seed for a decision-sheet row in Part B; record the evidence (file paths,
script lines) as you go.

- **HDL language.** SystemVerilog and Verilog work with the built-in engines
  (`sv2v` ships in the sandbox to convert SV→Verilog where an EDA tool needs it).
  Three buckets, and the third is the one that gets misread:
  - *SV/Verilog only* — green.
  - *VHDL-only, or genuinely **mixed**-language* (one design whose SV and VHDL
    parts must elaborate together) — red: no built-in engine reads VHDL.
  - *Twin-language* — the repo ships an SV tree **and** a VHDL tree that are
    independent implementations of the *same* unit (`verilog/` + `vhdl/`, often
    with a GHDL flow of its own). This is **not** mixed-language and **not**
    red: nothing has to elaborate across the two. Plan the SV twin, and record
    an explicit **scope-exclusion row** in Part B — "`vhdl/` is out of scope: no
    Target references it, it is not linted, simulated, or deleted", with the
    reason (no VHDL engine in the matrix) and the note that no functionality is
    lost because the twin implements the same design. Say it out loud in the
    plan; an unstated exclusion reads later as an oversight. Verify the twin
    claim before leaning on it (same module/entity names, same test vectors) —
    if the VHDL tree is a *different* unit the repo also needs, that part is red
    and belongs in the verdict table as such.
  Generator-based designs (Chisel, SpinalHDL, Amaranth/Migen, HLS) are
  feasible only if the emitted Verilog is captured as the design source: as a
  fileset, with the generator run by the post-setup hook (once per worktree)
  or by `pre_run_commands` (per test).
- **Current EDA tools.** Whatever the repo uses today (visible in its
  Makefiles, `*.f` file lists, `scripts/`/`flow/` directories, TCL, and CI
  configs) maps onto the supported EDA tools matrix (`docs/user/SUPPORTED-EDA-TOOLS.md`, read
  per the version rule in Part A's preamble). Verilator, Icarus, Yosys
  (+OpenROAD) and sv2v are built in. AMD Vivado 2025.2 is built in only
  through the Linux-x86-64 administrator-registered, host-provisioned policy.
  Xcelium and VCS parser modules are internal incubation material, not public
  simulator integrations; Xcelium, VCS, Questa/ModelSim, Design
  Compiler/Genus, SpyGlass, and the like are outside the matrix (red for that
  flow).
  **Not every script in the repo is a flow.** Sort what you find into three
  piles before mapping anything:
  - *Flow scripts* — they build, elaborate, simulate, lint, or synthesize.
    These map onto the matrix; everything else does not.
  - *Inert content* — exploration/plotting/analysis code (`scripts/*.py` that
    plots error curves, generates docs, sweeps parameters), examples, and
    scratch. It has **no bearing on any flow**: name it in one line of the plan
    as "present, not wired", and never let it seed a Target, a fileset, or a
    determinant verdict.
  - *Source-mutating helpers* — a repo's "check"/"format" step is often a
    **formatter that rewrites the tree in place** (`check/run.sh` running
    `verible-verilog-format --inplace`, `clang-format -i`, `astyle`). It looks
    like the repo's parse/lint gate and is not one: wiring it into a Booley flow
    would have a Booley Flow rewrite the design's own sources mid-run. **Never wire a
    mutating command into any flow, hook, or `pre_run_commands`.** If the repo's
    style is worth enforcing, that is the *non-mutating* linter of the same
    family (`verible-verilog-lint`) as the style-lint Target in row 11 — record
    the formatter as evidence for row 11, not as a flow.
- **Tech Cell Replacement.** For every enabled synthesis Target, identify the
  physical-library family supplied by the selected synthesis Flow and verify
  the actual Liberty input and, for physical mode, the LEF input. Treat the
  Flow's fixed reference-library choice as an evidence-forced Project Setup
  fact, not as a Target field. If Project requirements or a native Flow need a
  different family, mark the built-in synthesis Flow Red rather than claiming
  that its reference technology validates the Project's library.

  Build one Project inventory by following every source, include, generated
  input, dependency core, define, parameter, and synthesis script reachable
  from each enabled synthesis Target. Classify every finding as exactly one of:

  - **documented technology-integration seam**;
  - **direct library-cell instantiation**;
  - **behavioral primitive intended for inference or replacement**;
  - **existing synthesis-time binding or post-inference mapping**;
  - **other library-dependent cell use requiring review**.

  For each finding, record its defining file, owning hierarchy or dependency
  core, applicable Targets, governing define or parameter, and whether it is
  active for a Target or repository-only evidence. Repository-only evidence can
  explain intent but is not silently added to a Target. Preserve enough
  hierarchy and dependency-core provenance that partial top-level coverage
  cannot look complete.

  Group the inventory into one Project-wide **Tech Cell Replacement** mapping
  and a per-Target coverage matrix. The matrix names the subset used by each
  enabled synthesis Target, including embedded cores, and exposes every
  discovered-but-unhandled remainder. Counts are expectations to validate, not
  proof. For each entry, record the RTL intent, chosen library cell, mechanism,
  one authoritative Project-owned source location, frontend definition,
  semantic evidence, and required checks.

  A mechanism may preserve a valid direct instantiation, configure a documented
  technology-integration seam, or use an approved Project-owned hook and
  adapter module. When several mechanisms are credible, explain the hardware
  consequence of each and recommend one. Interactive mode asks the user to
  clarify the choice. Unattended mode selects the mechanism supported by the
  strongest Project evidence and records the resolution and confidence for
  review. Stop when ambiguity could change hardware semantics, leave hierarchy
  coverage incomplete, or introduce conflicting definitions; a reversible
  choice between otherwise valid mechanisms may be recorded as `review`.

  If no replacement mechanism exists, ask for approval to add a Project-owned
  hook or adapter module. If the RTL is vendored or approval is absent, record
  the incomplete mapping and mark every affected synthesis Target Yellow;
  Project Setup does not claim completion. Existing Project-authored
  post-inference latch maps are migration evidence only. Do not create a new
  post-inference mapping as a fallback. Reconcile `[flows.synth].expected_latches`
  with the evidenced intentional-latch remainder after replacement: it is an
  allowance, not evidence that replacement occurred.
- **Testbench style.** Booley scores a simulation by matching a stdout
  sentinel. A self-checking SV/Verilog testbench that prints a clear pass/fail
  line is green: its wording becomes config in Step 2. UVM is fine as long as
  it prints a pass/fail line. A **cocotb** (Python) testbench is also green —
  it runs on the built-in sandbox path as a Cocotb Target, with verdicts taken
  from cocotb's `results.xml` rather than from a sentinel. A
  *Makefile-orchestrated* sim usually reduces to a `.core` (the RTL/TB
  filelists) plus `pre_run_commands` (the per-test firmware/vector build the
  Makefile did before the sim). A directed testbench with no self-check has
  nothing to score until a sentinel is added.
  **Sentinel archaeology applies to SV/UVM testbenches only.** A Cocotb Target
  scores from `results.xml` and ignores `pass_sentinels`/`fail_sentinels`
  outright — do not go hunting for pass/fail wording in a Python TB, and record
  row 5 as `none — cocotb`. On a cocotb repo the equivalent up-front work is the
  **pin set** (row 7): the project's `tox.ini`/`requirements.txt` era decides
  whether it runs at all. For an SV TB, work the source **now** — every wrinkle
  below reads as INCONCLUSIVE, or worse as a false PASS, at the first real run.
  Booley scans **stdout**, so a TB that writes its verdict only to a log file
  via `$fwrite` (Ibex does) needs a project-owned adapter, wrapper, or monitor
  that relays the existing verdict before it can score. Treat vendored and
  upstream sources as preserved inputs: plan the verdict bridge outside them
  whenever that is sufficient. Editing an upstream testbench is a separate
  ownership decision that requires the user's explicit approval. And
  don't assume a tidy `PASSED` exists — some TBs signal success with wording as
  oblique as a final `10. Comparision` line (biRISC-V) and reserve clear strings
  for failures only. Enumerate **four** categories from the TB source, not the
  three that are obvious, and never by grepping for the word "pass":
  1. **pass** wording;
  2. **fail** wording (assertion/mismatch/error);
  3. **exception / timeout** wording (watchdog fired, `$fatal`, max-cycles);
  4. **input / setup error** wording — the TB *cannot start*: a missing vector
     or firmware file (`"<file> is not available!"`, `$fopen` returned 0, "cannot
     open"), a bad plusarg, an empty test list, usually followed by `$finish`.
     This is the dangerous category, and it is the one that gets skipped: such a
     run prints no fail wording at all, so unless the message is registered in
     `fail_sentinels` it scores **PASS** — and it scores PASS *especially* when
     the TB already printed a pile of per-case successes before the file it
     needed went missing. Grep the TB for `$fopen`, `$readmemh`, `$value$plusargs`
     and lift the exact wording of every early-exit path.
  **Then check how often the pass sentinel prints.** A per-case `TEST SUCCEEDED`
  that fires ~96× in one run means the whole verdict rests on the tie-breaking
  rule: **a fail sentinel wins ties** (CONFIG.md → sentinels). That is what makes
  a fail-dominant sentinel set safe with a repeating pass string — and what makes
  a *missing* category-4 sentinel fatal. Record in row 5 that the set is
  fail-dominant by design, so nobody later "cleans up" the fail list.
- **Non-scalar toplevel ports.** For `lint` and `synth`, read the
  port list of the module you intend to make the Target's `toplevel`. Two shapes
  matter, and they have **different** answers — do not apply the interface rule
  to a struct:
  - **SystemVerilog interface ports** (`my_axis_if.snk s_axis`) — the module
    cannot serve as a standalone lint/synthesis top, so both flows are dead until you add a thin
    **flat-port wrapper** (one small file, not a blocker; `booley doctor` flags
    it at setup time). `sim` is unaffected: a cocotb testbench brings its
    own wrapper. Symptom and wrapper recipe: Booley's `docs/user/TROUBLESHOOTING.md` ("interface
    parameter mismatch").
  - **Packed-struct / user-typedef ports** (`input fp_operation_type op`, a
    `typedef struct packed` or an enum from a package) — **no wrapper needed.**
    A packed struct is just a bit vector with names on it; the frontends flatten
    it. Plan the Target straight onto the real toplevel. What this shape *does*
    deserve is proof, because it is a known frontend-gap poker (the ravenoc/taxi
    shape): add an **execution-time check** that the frontend reads and lowers
    the struct-ported toplevel — the `synth` Target's own RTL frontend is
    the check (sv2v by default; the fallback is Target
    `flow_options.frontend: slang`, or `--frontend slang` for a
    one-off). Record the row at `medium` confidence with the check attached, not
    at `low` with an invented wrapper. Only if that check fails does a wrapper —
    or the slang frontend — come into play, and that is the deviation rule's
    business, not a pre-emptive one.
- **Compiled software artifacts.** Does the testbench need pre-built software
  to run — a firmware `.hex`/`.bin`, a `$readmemh` memory image, other
  toolchain-generated inputs? This is the single biggest hurdle when porting a
  **CPU core**: the TB boots a program the repo expects you to compile. Look
  for the tell-tales: a `$readmemh`/`$readmemb` in the TB, a `firmware/`,
  `sw/`, `fw/`, or `tests/` dir with `.c`/`.S`/`.asm` sources, a `Makefile`
  target that emits `.hex`/`.mem`/`.bin`, or a cross-compiler prefix
  (`riscv*-`, `arm-none-eabi-`, …) in the build scripts. If found, this is a
  **required toolchain**, not a data file — plan to bake it into the sandbox
  image and build the artifact on demand (see the worked example in the
  appendix), never to vendor a prebuilt blob. And don't stop at "a RISC-V
  toolchain exists in the image" — plan an execution-time check of the
  project's exact compile flags against the sandbox compiler, because vendor
  `-march` strings are a classic trap (Booley's `docs/user/TROUBLESHOOTING.md`, "RISC-V
  firmware won't assemble against the sandbox GCC").
- **Repository shape.** Three topology traps are worth ten minutes of looking
  before any config is planned. *Self-referential symlinks*: some repos ship
  links that make the tree infinitely recursive for any walker that follows
  them — run `find . -type l` early. *FuseSoC multi-core repos*: count the
  `.core` files and duplicate target names (Ibex: 208 cores declaring `lint`
  54 times) — you'll be qualifying explicit Flow targets as `vlnv#target` and
  marking Doctor selections in the intended declarations, and colliding vendored cores are
  usually required dependencies, so `FUSESOC_IGNORE` can't hide them.
  *Upstream targets aren't automatically trustworthy*: CAPI2's YAML-anchor
  idiom (`<<: *default_target` with a `filesets:` override) **replaces** the
  fileset list rather than merging it, and upstream's own targets can be
  silently broken by it. *A published `.core` ships a `provider:` block*
  (`provider: {name: github, …}`) that makes fusesoc re-download the core on
  **every** local run — a `403` through the egress proxy whose error names
  neither the block nor the fix; plan to delete it (Step 2's upstream-`.core`
  trap list). Real `fusesoc` validation needs the sandbox, so
  record a planned execution-time check rather than trusting upstream targets
  now — or plan to author `booley_*` targets with explicit filesets. **Write
  the check in raw-fusesoc syntax**, which is not Booley's:

  ```
  fusesoc --cores-root <dir holding the .core> run --setup \
          --work-root "$(mktemp -d)" --target <target> <vlnv>
  ```

  The `<vlnv>#<target>` qualifier is a **Booley-surface spelling only** (it is
  how explicit Booley Flow calls name a Target); raw fusesoc
  rejects it with `Illegal character in core name`. `--cores-root` is a
  *global* flag and must come **before** `run` — after it, fusesoc 2.4.6 exits
  with `unrecognized arguments: --cores-root`. For a stealth authored core the
  dir is `.booley_project/cores`; for an in-tree `.core` it is the repo root.
- **Git submodules.** Run `git submodule status` in the outer repository and,
  when `.booley_project` is a standalone Git repository, run
  `git -C .booley_project submodule status` too. If either has any, add a
  decision row — but a short one: Goal worktrees get their submodules
  reconstructed from local Git objects at the same repository path, never
  cloned, so private SSH URLs are not consulted (mechanics in CONFIG.md →
  "Submodules"). What the plan owes is the precondition and, if the outer repo
  has heavy submodules nothing builds against, the explicit list:
  - Every selected submodule must be **present, clean, non-shallow, and contain
    the required pinned commit objects** in its owning repository before any
    Goal work — host-side `git submodule update --init --recursive` in the
    outer repo and `git -C .booley_project submodule update --init --recursive`
    in a standalone paired repo, with no uncommitted submodule work left.
    Worktree setup hard-errors otherwise with the missing path, dirty checkout,
    shallow history, or incomplete local-object cause.
  - Destination gitlinks are authoritative, so historical pins are preserved.
    `.gitmodules` discovery is the default and needs no config. To materialize
    only some outer-repository entries, set `[submodules].paths` in
    `booley.toml`; an explicit empty list selects none, and a non-empty list is
    intersected with the selected revision's gitlinks. Every top-level gitlink
    in a paired project repo is materialized. Nested gitlinks are recursive.
  - Execution-time check: a Goal worktree comes up with the submodule
    populated (the main checkout looking fine proves only the precondition).
- **Design scale.** Past ~250 files or ~150K LOC (`booley doctor` prints a
  NOTE at that scale), plan for it: an early ingest smoke (an `iverilog`
  compile of the full filelist) at execution time to prove the RTL is even
  readable before config is authored around it, a small sub-block as the
  synthesis smoke target rather than the full top (a full-chip flatten can OOM
  a 30 GB host), and an explicitly named fast test pinned as the sim smoke so
  `doctor --deep` doesn't wander into a multi-hour full suite.
- **Encrypted, vendored, or PDK-locked IP.** Encrypted RTL that the supported
  Verilator/Icarus paths cannot read makes simulation red; Booley has no public
  licensed-simulator integration.
  Vendored cores you'd rather Booley not discover can be quarantined with a
  `FUSESOC_IGNORE` marker; those aren't blockers. Synthesis against a real
  foundry PDK (rather than the built-in reference physical-library flow) is
  outside the built-in flow (red for that flow).
- **License reachability.** A licensed-EDA-tool Flow is only real if the
  administrator can register an approved License Profile and its fixed server
  answers. The Sandbox must receive licensing only through Booley's
  policy-owned relay; Project configuration cannot supply a license endpoint.
  If the approved server is unreachable indefinitely, that flow is red, not
  yellow: a working wrapper is worth nothing without a license the EDA tool can
  check out.

### Output: the verdict table

Part A ends in a per-flow verdict table:

```
Flow   Verdict  Provisioning                       Why
sim    Green    image (Verilator/Icarus)           SV TB, self-checking
lint   Green    image (Verilator)                  SV RTL
synth  Yellow   image                              no upstream SDC; user must supply
fpga   Yellow   host-provisioned Vivado 2025.2     registration/grant pending
```

Confirm with the user which of the feasible flows they actually **want** —
that, not mere feasibility, decides what gets configured. Green and yellow
flows are configured in Step 2; red and unwanted flows use
`[flows.<name>].enabled = false`.

**Unattended fallback for want-ness (row 1).** Nobody is there to say what they
want, and want-ness has no codebase signal, so it resolves like the other
never-evidence-forced rows: **configure every Green flow, plus any Yellow one
whose remaining wiring the plan can fully specify from evidence** (the exact
command, the exact file to author). Leave out Yellow flows that hinge on
something only the user can supply (a license host, a host EDA-tool install,
an SDC or XDC the repo does not ship) and all Red flows. Star row 1 `review`
per flow-set — "configured sim/lint/synth; fpga left out (Vivado present but
untargeted)" — and surface it in the final report. Configuring a flow the user
did not want is cheap to drop later; silently skipping a flow they wanted is
the failure this fallback exists to avoid.

## Part B — Decision sheet

Turn the evidence into decisions. Every row carries: **decision · proposed
value · resolution mode · confidence (high/medium/low) · evidence (file paths /
script lines) · open question (if any)** — resolution and confidence are
separate columns (see "How a row resolves"). The standard checklist:

1. **Flows to configure** — from Part A plus the user's intent; per flow:
   enabled or not (`enabled = false` is the explicit opt-out), plus the Target
   it drives. Every enabled Flow executes inside the Sandbox. Record
   any approved commercial provisioning separately in row 14.
2. **`.core` ownership/placement strategy & Target set** — decide this together
   with row 16's git footprint. The placement is deterministic:
   - **Open footprint + native `.core` exists:** reuse the appropriate native
     core. Convert the selected EDA-tool-API Target in place or add the needed
     modern Target to that core; do not create a parallel Booley core. Preserve
     unrelated EDA-tool-API Targets unless the plan explicitly puts them in scope.
   - **Open footprint + no native `.core`:** author a normal tracked project
     core at the repo root or beside its RTL.
   - **Stealth footprint:** never edit the repo's tracked native cores. Author a
     distinct-VLNV core under `.booley_project/cores/` with repository-root-relative
     fileset paths. Booley projects ignored root-level copies for FuseSoC; do
     not create source-resolution symlinks. Native-core modernization findings
     outside the Doctor-selected Target surface are notes, not setup work.
   - **Hybrid integration footprint:** use only when the user or an enclosing
     port workflow explicitly requires it. Keep operational `.booley_project/`
     state local and ignored, while tracking the minimal repository-native
     integration artifacts the project must retain (selected `.core` Target
     edits, constraints, wrappers, and the setup/port report). This is not the
     stealth-core layout: the tracked native core remains authoritative.
   When several native cores could own the new Target, mark this row for user
   review instead of guessing. A hidden authored core requires row 20's
   `[stealth] enabled = true`; non-stealth projects use tracked native cores.

   **Enumerate the full
   Target list here**, not just a name per intent: each Target with its intent,
   toplevel, and test list. The counting rule depends on the TB flavor (row 4);
   `CONFIG.md` is authoritative:
   - **Classic sentinel SV/UVM TB** — one Target per *intent* (sim / lint /
     asic / fpga). A distinct config (parameter/define set) or a distinct
     toplevel gets its own Target; nothing else does.
   - **Cocotb** — **one Target per test module**, always. `cocotb_module` is a
     per-Target flow option, so a second test module is structurally a second
     Target: 8 test modules ⇒ 8 sim Targets. This is not over-authoring, it is
     the only shape that runs.
   - **Neither flavor gets a Target per submodule** — that is over-authoring
     for setup.

   **Matrix scaling.** Config variants *multiply* the module count (8 cocotb
   modules × 3 define flavors = 24 sim Targets), and every one of them is a
   `.core` target and a `tests.toml` section. Only targets explicitly marked in
   `flow_options.booley.doctor` join the `doctor --deep` matrix.
   Do not author the full matrix at setup: pick the **one baseline flavor** the
   project actually verifies today, author that row of the matrix, and record
   the rest as a deferred follow-up (adding a flavor later = duplicating the
   module Targets with its define set). Name them `sim_<module>` and, only when
   a second flavor lands, `sim_<module>_<flavor>`. If the baseline row alone
   still exceeds ~12 Targets, make the cut a **grill question** rather than
   generating them.

   Where the set is under-determined (which configs matter, which toplevel is
   the real one), it's a **grill question**. Record `vlnv#target` qualifiers for
   explicit callers and mark the intended per-core Doctor targets.
   **Lint each planned Target name against the axis convention before
   approval.** Every Booley-authored Target name must be `<axis>_<subject>`,
   lowercase snake_case, where `<axis>` is one of the four fixed tokens
   `sim` / `lint` / `synth` / `fpga` (the Booley Flow family). The axis is not derivable from
   `.core` metadata, so the name must carry it. Reject plausible-but-wrong
   names now rather than at the Step-4 doctor NOTE: `asic_core` is wrong
   (`asic` is not a naming axis — use `synth_core`);
   `synthesis`/`impl` are likewise not axis tokens. A vendored upstream `.core`
   keeps its upstream Target names and is exempt.
3. **Toplevel(s)** — per intent; **flat-port wrapper needed?** (from the
   non-scalar-ports determinant: interface ports ⇒ yes, packed-struct ports ⇒
   no + an elaboration check).
4. **Testbench flavor** — `sv`, `cocotb`, or `mixed` with a default (a project
   convention fixed at setup — **always a grill question**, never
   inferred silently).
5. **Sentinels** — the exact pass, fail, timeout/exception, **and input/setup-
   error** wording lifted from the TB source (all four categories from the
   testbench-style determinant), or the decision to insert Booley's markers.
   Note in the row that the set is fail-dominant (a fail sentinel wins ties) and
   why that matters here — a pass string that prints once per case makes it the
   only thing standing between a missing input file and a false PASS. **N/A for
   Cocotb Targets** — the verdict comes from `results.xml`; a sentinel there is
   dead config. Write `none — cocotb` and move on.
6. **Test list** — which tests go in `tests.toml`, the runtime selector shape,
   and the pinned fast smoke test for large designs.
   **Pin the smoke test provisionally when you cannot time it.** The probing
   rules forbid running a sim in Step 0, so a host-side pick is a guess — and
   the obvious heuristic is wrong often enough to plan around: *fewest
   tests/smallest vector set ≠ fastest* (a small div/sqrt set is multi-cycle per
   op and can run 30× longer than a big single-cycle one — measured 12.4 s vs
   360.6 s on the same design). So: pick a candidate, write the value as
   `<target> (provisional — re-pin from measured timings)` at `medium`
   confidence, name **all** the candidates you considered, and add an
   execution-time check — "time each candidate Target in Step 2 and re-pin the
   smoke to the measured fastest". Re-pinning from a measurement is a *minor*
   deviation (log one line in §3), never a stop-and-ask.
   **Does any test need a non-RTL build step before it can run** (per-case
   firmware compile, vector staging)? — use repo evidence or the defaults
   block; ask when the build recipe is unresolved. If yes, the
   command lines become `[flows.sim].pre_run_commands` and the toolchain
   they need goes into the sandbox image (row 7). Two shape constraints to plan
   against, both from CONFIG.md: `pre_run_commands` and `run_cwd` live under
   `[flows.sim]` and are therefore **global to the Flow — one value shared
   by every sim Target**, not per-Target knobs. Per-Target behavior has to come
   from *inside* the commands, branching on the exported `$BOOLEY_TARGET` (also
   `$BOOLEY_TEST_NAME`, `$BOOLEY_TEST_NAMES`, `$BOOLEY_RUN_CWD`). And `run_cwd`
   is one directory for all sim Targets, relative to the repo root — if two
   Targets need different input dirs, the commands stage into the one `run_cwd`,
   they do not each get their own. (The commands themselves run from the **repo
   root**, not from `run_cwd`, and nothing auto-creates directories — a staging
   script `mkdir -p`s its own target dir or `cd "$BOOLEY_RUN_CWD"` first.)
7. **Sandbox image** — base `booley-sandbox`, prebuilt `booley-sandbox-riscv`,
   or a project Dockerfile; driven by the toolchain determinant (firmware
   cross-compilers, `srec_cat`, Python dep pins for cocotb 1.x TBs, …).
   **Python dependencies alone use `[sandbox].pip_requirements`.** Point it at
   the repo's pinned requirements input and let `booley init` generate the
   project image layer. Choose a hand-authored project Dockerfile only when the
   project needs non-Python packages or EDA tools, or custom build steps that
   `pip_requirements` cannot express.
8. **Data files & built artifacts** — vendor genuinely static inputs
   (`file_type: user` + `copyto`, force-add if upstream gitignores them) vs
   build-on-demand via a `post-setup` hook for anything a compiler emits.
9. **Vendored cores** — which directories get a `FUSESOC_IGNORE` quarantine
   marker, and which colliding cores are required dependencies that can't be
   hidden.
10. **Constraints** — physical ASIC synth needs an SDC per synth Target that
    creates at least one clock (hard error without one); FPGA needs an XDC
    fileset. Timing constraints encode design intent that only the design
    owner knows: clock periods, which clocks are asynchronous, which clocks
    leave the design, and I/O budgets. **Never author, generate, or guess
    one.** Resolve the row from exactly one source:

    - **The repo ships it.** Record its path and reference it in place from the
      Target's constraints fileset. Use it as-is. Do not edit, relax, or add
      exceptions to it (periods, false paths, clock groups) unless the user
      explicitly asks.
    - **The user supplies it.** If the repo ships none, the Target is
      **blocked** until the user provides a file. Interactive: ask for it in
      the grill (a path, or file contents to place at
      `.booley_project/cores/constraints/<target>.sdc` or `.xdc`). Record the
      exact file as `user-confirmed`. Copying a file the user supplied into
      the project is not authoring.
      Unattended: leave that Target unconfigured. Star the row `review`, and
      name the missing file in the final report as an open question.

    Offer a real alternative while the file is missing: `synth_mode: logical`
    needs no SDC and gives mapped area and an approximate Fmax. Offer it as a
    choice; never switch to it silently. An SDC that is only a placeholder
    (for example a single made-up clock) is still authoring and is forbidden.
10a. **Memory implementation** — for every enabled ASIC synthesis Target,
    scan its reachable RTL and native synthesis scripts for instantiated
    SRAM/RAM/register-file modules, large unpacked arrays,
    implementation-selection defines/wrappers, and exported memory ports.
    Record each candidate's source evidence, logical size, interface and clock
    domains, synchronous/asynchronous read kind, visible latency, replacement
    seam, one disposition (`exported_boundary`, `timing_surrogate`, or
    `blocked`), and confidence.

    An exported boundary retains Target-authored SDC I/O intent. An internal
    memory may use a timing surrogate only when its project-owned
    replacement seam and timing shape are understood; scale alone never
    decides the disposition.
    Unsupported asynchronous reads, ambiguous collision-visible behavior,
    unsupported independently clocked/multiported topology, or no source-level
    replacement seam are `blocked` rather than guessed.

    Use a safe read-only Yosys warning/statistics probe when one already exists,
    but do not run a synthesis that can expand a substantial unclassified
    memory. The synth verdict is Yellow while an adapter or timing semantics
    still need authoring/confirmation, and Red for that Target when no safe seam
    exists. RTL elaboration alone never makes this row Green. An enabled synth
    Target with any unclassified candidate cannot be approved.
11. **Style lint** — offer Verible style lint as a second lint Target only if
    the user wants it (offer, never impose). "Never impose" means
    never inflict a *foreign* style on a repo — it does not mean ignoring the
    repo's own. **A strong in-repo signal makes this an ordinary inferred row:**
    a `.rules.verible_lint`/`verible.filelist`, a CI job running
    `verible-verilog-lint`, or a format/check script driving
    `verible-verilog-format` (see the source-mutating-helper pile above) all say
    the project already lints with Verible. With such a signal, propose `yes`
    with that evidence and reuse the repo's own rules file as a
    `file_type: veribleLintRules` fileset entry — you are matching the project,
    not imposing. Unattended: signal ⇒ `yes` (`inferred`/high, not starred);
    no signal ⇒ `no`, and say so in one line.
12. **Elaboration Check** — record whether the project needs the stronger
    `sim --mode elab-only-standalone` module sweep; ordinary Simulation already
    records its authenticated build-stage outcome.
13. **Timeouts, synthesis calibration & memory** —
    `[flows.<flow>].timeout_ms` where evidence (CI runtimes, log stamps)
    suggests the defaults will not fit. Mark every supported synthesis
    configuration that Doctor must validate with `booley: {doctor: [synth]}`.
    Step 4 synthesizes the complete marked matrix end-to-end and retains the
    largest measured boundary-command process-tree peak RSS to settle
    `[jobs].heavy_memory` and `[sandbox].memory`. Record an execution-time check
    that every intended matrix member ran; do not guess one representative from
    filename or LOC.
14. **Commercial EDA authority** — for host-provisioned Vivado: the registered
    installation, optional License Profile, exact Project Grant, approved test
    window, and who confirms the policy works. Never plan a host command path.
15. **AGENTS.md** — wanted? If a canonical `AGENTS.md` already exists, its
    fate (merge / overwrite / leave); any project gotchas the user wants
    recorded (Step 3 only writes gotchas that came from an instruction file or
    from the user — collect them here, not mid-execution).
16. **Git footprint — Keep Booley out of your git history?** **Always a grill
    question**, merged with row 20: ask exactly **"Keep Booley out of your git
    history?"** Explain both effects before taking the answer:
    - **Yes (hidden):** `.booley_project/` stays **untracked** in the RTL repo,
      excluded through the parent repo's `.git/info/exclude` (never tracked
      `.gitignore`), **and stealth mode turns on**: the commit-message scrub
      removes protected AI/tool names from new commit messages, and hidden-core
      projection makes ignored root copies when Booley authors cores.
      Row 16 = `hidden`, row 20 = `enabled = true`; both are `user-confirmed`
      from this one answer, including for hidden config-only projects.
    - **No (open):** `.booley_project/` is **committed** to the RTL repo like
      other project config, builds use tracked native cores, and row 20 =
      `enabled = false`. Both rows are `user-confirmed` from this answer.
    Recommend Yes/hidden for a port (matching `booley init`'s footprint), and
    No/open for greenfield. Step 4 executes whichever footprint this row says.
    **Existing hand-set `[stealth]` wins:** keep row 20 `pre-set` verbatim,
    explain its effects alongside the footprint answer, and reopen it only if
    the proposed core layout directly contradicts it.
    **Hidden without the scrub** is a follow-up only if the user volunteers
    that preference; never offer it unprompted. It is valid only for a
    config-only project: resolve row 20 `enabled = false`, flag that the hidden
    project dir is versioned nowhere, and author no hidden cores. Hidden
    authored cores require stealth's projection and scrub together.
    **Hybrid** is reserved for an explicitly requested port/integration policy:
    keep `.booley_project/` hidden and stealth enabled, but track only the named
    native cores, constraints, wrappers, durable root `AGENTS.md`, and report
    required by that policy. Record the exact tracked allowlist in this row;
    keep operational state hidden.
    Once hidden is settled and native `.core` files exist, ask exactly:
    **"Should Booley ignore the repository's existing `.core` files and use
    only the stealth-authored cores?"** Record `ignore_native_cores = true`
    only from an explicit yes, and only with stealth enabled. Recommend yes
    when evidence shows the native cores fail the installed FuseSoC schema or
    cannot express the selected Flow Targets; otherwise recommend no. Explain
    that the switch affects Booley resolution, not raw
    `fusesoc --cores-root <repo>` commands.
    **Unattended:** keep row 16 `hidden` (init's default), starred `review`;
    row 20 follows its unattended rule below, also starred `review` unless
    pre-set. Hidden alone supplies no consent to rewriting commit messages.
17. **Specialists** — a **defaults block** row: default = none disabled.
    Every installed Specialist is discovered automatically; Step 2 writes
    `[specialists.<name>].enabled = false` only for an intentional opt-out.
    Show optional reviewers and mutation testers with that proposed default.
    One reason to change it: **`mutation_tester` has not supported cocotb-based
    sim Targets** — its baseline runner drives a `V<toplevel>` binary, and a
    Cocotb Target builds `Vtop` driven from Python over VPI. Verify current
    support before using it on a cocotb project; unsupported, it burns a full
    specialist run to report an infra error. Unattended: take the default as
    `inferred`/high, **no star**; an existing opt-out stays `pre-set`.
18. **Parity check (optional)** — a **defaults block** row: compare Booley's
    results against the repo's native build system after the Step 4 gate.
    Comparable only per phase where Booley and the native build use the
    **same EDA tool** (native VCS vs Booley Verilator → `none`, evidence-forced).
    Default `none` where no runnable native flow exists; see `steps/5-parity.md`.
    When an identical native sim script exists (same EDA tool, design, and TB),
    propose `sim` in the defaults block with the script as evidence. This is a
    proposed default, not an individual question. The first cut compares sim
    verdicts and cheap telemetry; it is optional and never blocks completion.
    Unattended: take the default, **`sim`, not `none`** for that identical-tool
    case, otherwise `none`; record `inferred` with confidence, **no star**
    (or `evidence-forced` where no tool matches).
19. **Agent backend (provider)** — preserve the provider and auth policy that
    `booley init` already recorded in `[agent]`. Record the row as `pre-set`
    with that table as evidence; the setup plan does not re-litigate it. A
    Project may omit one of these fields. In that case ask only for the
    missing choice and record it explicitly: the codebase cannot reveal which
    account the user intends to bill, and neither provider may be inferred.
20. **Stealth mode (`[stealth]`)** — **always a grill question**, answered by
    the one merged row-16 git-history question, not a second prompt.
    Interactive hidden → `enabled = true`, including config-only projects;
    open → `enabled = false`. Preserve an existing hand-set `[stealth]` block
    as `pre-set`, including when it differs from those interactive defaults.
    A commit-msg hook scrubs a configured banned-word list (`claude`,
    `anthropic`, `codex`, `booley`, …) from new commit messages; it can also cap
    the body (`max_body_lines`) or allowlist author identities
    (`allowed_authors`). Authored hidden cores remain self-contained under
    `.booley_project/cores/` and are projected into ignored RTL-root copies.
    **Unattended: write `enabled = false`** unless a hidden core is authored,
    which requires `enabled = true`; star the choice `review` unless pre-set.
    Nobody consented to a commit-message rewrite, so hidden config alone never
    enables the scrub in unattended setup.
    `booley init` creates `.booley_project/`'s own inner git repo *only* while
    `[stealth] enabled` is on. With the scrub off, a hidden-footprint project
    dir is versioned nowhere until someone `git init`s it — flag that for
    Step 4's footprint work rather than letting the combination pass silently.
23. **Tech Cell Replacement** — one Project-wide mapping shared by all enabled
    synthesis Targets, with each Target recording only the subset it reaches.
    Record the Flow-supplied physical-library family and verified Liberty/LEF
    inputs, the authoritative replacement location, the classified Project
    inventory, the per-Target coverage matrix, each semantic decision and
    frontend definition, approved Project-owned inputs, incomplete or Yellow
    Targets, open questions, and execution-time checks for every validation
    layer. When synthesis is disabled, resolve this row as
    `evidence-forced: not applicable` and omit the replacement subsection.
    Never satisfy this row with a list of cell names alone.

Immediately after the decision sheet, write the row-23 subsection in the plan
with these headings: **Flow/library and authoritative location**, **Project
inventory**, **per-Target coverage matrix**, **replacement table and semantic
decisions**, **approved Project-owned inputs**, **incomplete/Yellow Targets and
open questions**, and **execution-time checks**. The replacement table must
name each entry's RTL intent, cell, mechanism, source location, frontend
definition, semantic evidence, and validation layers; the coverage matrix must
retain hierarchy and dependency-core provenance.

Then add the **repo-specific rows**, numbering on from 24 — everything Part A
surfaced that the standard list doesn't name: generator steps, **git
submodules** (a row whenever either participating repository's
`git submodule status` is non-empty: the host-side
initialized/clean/full-history precondition, and `[submodules].paths` if only
some outer gitlinks should reach Goal worktrees), **scope exclusions** (a VHDL twin, a subsystem nobody
targets — say what is excluded and why, never leave it implied), multi-clock
timing intent, environment modules, a TB stdout tee, unusual directory layouts.
The checklist is the floor, not the ceiling.

Close with the **execution-time checks** list: every planned verification that
needs the sandbox (fusesoc target resolution, `-march` compile check, ingest
smoke, image EDA-tool probes, submodule population in a Goal worktree). Steps
2–4 run these; include semantic, frontend, mapped-netlist, and physical-link
checks for every enabled synthesis Target, plus CDC preservation checks when
applicable. A failed check that contradicts a decision triggers the deviation
rule.

## Part C — The grill (interactive mode only)

This is the skill's most user-facing moment, so the **onboarding voice**
(SKILL.md) is in full force: assume the user is new to Booley. Every question
carries its recommended answer and a plain-English reason it matters, and any
Booley term gets defined verbatim from `../GLOSSARY.md` the first time it
appears. Ask by the template’s plain label, with row number/internal key only
in parentheses — a user who does not yet
speak Booley still has to make every call here.

Refine the decision sheet with concrete options and their tradeoffs:

- **Map the open rows as a dependency tree.** A decision branches into every
  decision that depends on it. The current **frontier** is every unresolved
  decision whose prerequisites are already settled. High-impact choices such
  as flow routing, TB flavor, image/toolchain, and the **agent backend** (row
  19, `claude` vs `codex`) naturally sit near the roots because reversing one
  late would invalidate much of the sheet.
- **Ask the whole frontier in one round**, then wait for the user's answers.
  Every question carries a recommended answer, the evidence behind it, and the
  plain-English trade-off. If one question depends on another question still
  open in the current round, defer it to a later round instead of mixing
  dependency levels.
- **After each response, recompute the frontier.** Record settled decisions;
  leave unanswered decisions open rather than silently inferring them. Settled
  roots expose their downstream questions for the next round.
- **The mandatory rows are non-negotiable.** Always asked: row 4 (unless
  evidence-forced), merged rows 16 + 20; row 19 only when init left a field
  unset. Hand-set values stay `pre-set`. Everything else: evidence-forced or
  pre-set rows receive one confirmation line; defensible-default rows enter
  the defaults block; low-confidence or no-defensible-default rows become
  questions.
- **The defaults block.** Present every expert row that is not evidence-forced
  or pre-set and has a defensible default as **one confirm block**: a table
  with plain label, proposed default, and one-line why. End with:
  **"Accept these defaults, or name the ones to change."** A row leaves the
  block and becomes a question when its inference confidence is `low`, there
  is no defensible default, or its value depends on an unanswered frontier
  question. Examples: undetected sentinels (row 5), ambiguous memory evidence
  (row 10a), missing constraints (row 10), several native cores that could own
  a Target (row 2), or more than ~12 Targets (row 1). Resolve dependencies
  before presenting the block: put it in the same round as the always-asked
  questions when independent, otherwise the next round; rows depending on
  row 4 or row 16 wait for those answers. This preserves the frontier rule
  against mixing dependency levels. Rows the user names to change become
  `user-confirmed`; accepted defaults are `inferred` at stated high or medium
  confidence. Unattended: take those defaults with confidence, without a star.
- **Codebase first**: never ask what the repo can answer. Ask to *confirm*
  low-confidence inferences, to *choose* where evidence genuinely
  under-determines (TB flavor, the Target set / config variants, style lint,
  host-EDA-tool placement), and to
  *supply* what only the user knows (license servers, which flows they care
  about, host EDA-tool installs).
- **Proportional depth**: a clean single-core Verilator repo needs two
  or fewer questions plus one defaults block; a 400K-LOC multi-core repo with
  firmware deserves a real session.

Format every question like this:

```md
❓ **Q1** - **<question title>**: <question body, including choices when useful>

➡️ <recommended answer, the supporting evidence, and why>
```

Stop only when the frontier is empty and no row remains silently assumed:
every row is `evidence-forced`, `pre-set`, `user-confirmed`, or `inferred` at
stated high or medium confidence (including accepted from the defaults block).
Low-confidence rows never land in that block. Summarize the resulting shared
understanding and ask the user to confirm it. Do not write the plan or move to
Part D before that confirmation.

## Part D — Write the plan and get approval

Fill `../SETUP_PLAN_TEMPLATE.md` and write it to
`.booley_project/SETUP-PLAN.md`:

- **§1 Feasibility** — the per-flow verdict table + determinant evidence.
- **§2 Decision sheet** — plain label first, with each Booley term defined
  verbatim from `../GLOSSARY.md` on first use. Split the finished rows into
  **Decisions you made** (`user-confirmed`, and unattended `review` rows),
  **Defaults accepted** (defaults-block rows, `inferred`), and
  **Settled by your repo or existing config** (`evidence-forced`, `pre-set`).
  Use those three `###` headings, the same nine columns, and global numbering;
  repo-specific rows also need a plain label, explanation, and internal key.
  Include the execution-time checks list.
- **§3 Approval & deviations** — the approval record; the deviation log starts
  empty and is appended by execution steps.

The standard decision rows also include setup-artifact retention and Flow-cache
disposition. `minimal` is the recommended default: preserve configuration,
authored integrations, reports, Findings semantics, and structured evidence;
remove only current-run manifest-owned scratch, duplicate captures, and
reproducible products. `diagnostic` retains raw current-run evidence while
still removing disposable probes. `preserve` is the recommended cache mode;
`evict-setup-touched` is explicit cache eviction through the Flow owner and
must disclose its rebuild cost. Unattended planning writes those defaults
without a late approval gate.

**Interactive:** show the user the complete plan and ask for exactly one
action: `approve`, `edit`, or `cancel` (default `cancel` on ambiguity). On
`approve`, set `status: approved` and continue to execution.
**Unattended:** set `status: auto-approved`, leave the `review`-flagged rows in
place, and continue. Do not stall. The approval line records the contract:
auto-approved, `N` rows starred `review` for the user to audit, the rest
`evidence-forced`, `pre-set`, or `inferred`. If *every* mandatory row came back
`review` (4, 16, 20, and any missing row-19 choice), say
so plainly in the final report — that is a plan the user has to read, not a
setup that ran itself.

Before writing an unattended plan, self-audit three mechanical invariants:

- no row says `user-confirmed` (nobody answered a question);
- every row containing independent choices has one resolution per item or is
  split into sub-rows; and
- a Python-only image decision uses `[sandbox].pip_requirements`, not a
  hand-authored Dockerfile.

From here on, Steps 1–4 consume the plan under the deviation rule in
`SKILL.md`: a plan-invalidating contradiction stops for the user; a minor one
is fixed and logged in §3.

Before execution begins, allocate one run-owned scratch root with
`booley cleanup prepare`. Record its run ID, scratch root, and manifest path in
the plan's §3 **Execution ledger**. Every setup-authored detached capture,
exit-code sidecar, probe, and conversion belongs beneath that root and is
registered with `booley cleanup record` before Step 7 can consider it. A
plan without the ledger remains readable but has no deletion authority.

## Appendix — worked example: a RISC-V CPU core's boot software

CPU cores are the hardest case, so here is the full pattern (from the lowRISC
Ibex port). The testbench boots a compiled program, so the sandbox image needs
the cross-toolchain and a hook that builds the firmware: *ship the toolchain,
not the frozen artifact*. In plan terms: checklist row 7 picks the image,
row 8 picks build-on-demand, and the execution-time checks list carries the
compile-flag probe.

1. **Toolchain layer.** For RISC-V cores (ibex, picorv32, biriscv, …) there is
   a ready-made **`booley-sandbox-riscv`** image (base sandbox plus a multilib
   RISC-V GNU toolchain, `srec_cat`, Spike, and the offline spec set). Point
   the project at it with the normal image selector:

   ```toml
   # .booley_project/booley.toml
   [sandbox]
   image = "booley-sandbox-riscv"
   ```

   For the image contents, how to pull or build it, and how to layer extra
  EDA tools on top with `# booley:keep` (needed when a repo like ibex also bakes
   Python deps), see CONFIG.md → "RISC-V toolchain image".

2. **Post-setup hook**: build the firmware on demand instead of committing a
   `.hex`. A Project setup script can run inside the container after a Goal
   worktree is created, before entry; point it at the repo's software build (Ibex's is a
   Make target):

   ```bash
   # .booley_project/hooks/post-setup.sh — the riscv toolchain is on PATH
   make -C <path/to/sw-build-dir> ARCH=rv32imc_zicsr   # e.g. Ibex's coremark make target
   ```

   The explicit `_zicsr` is not cosmetic — the sandbox GCC won't assemble an
   older project's default `-march=rv32imc` without it. See Booley's
   `docs/user/TROUBLESHOOTING.md` ("RISC-V firmware won't assemble against the sandbox GCC")
   for that and the vendor-ISA `-march` trap.

   Keep the hook thin — have it call a **tracked** script in the repo
   (`booley/build_firmware.sh`) rather than holding the build recipe itself.
   The hook lives under `.booley_project/hooks/`, which is ignored by
   convention, so a recipe written only there does not survive a fresh clone
   and the worktree comes up with no firmware.

   Then reference the built ELF/`.vmem` from the Target (a `.core` `files:`
   entry) or the sim's runtime selector. Doctor (Step 4) warns if a referenced
   firmware file is present on disk but untracked, or if a committed artifact
   looks built from in-repo source; both nudge you toward this build-on-demand
   pattern.

## Goalset policy in the plan

Record which seeded Goalsets need Project-specific rules. Ask whether a
`goalsets/default.md` should apply to every Goal entry, and record the answer
with the existing verification intent. Initialization seeds four Goalsets
create-only and does not create a default. Also record any requested opt-out of
the default Dashboard task with `[sandbox].dashboard=false`.
