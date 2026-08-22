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
  points at but does not force. **Rows 16 (git footprint), 17 (specialists),
  19 (agent backend), and 20 (commit-message scrub) can never be
  evidence-forced** — no codebase signal exists for any of them. Interactive:
  grill them. Unattended: take the documented fallback, star the row `review`,
  and surface it in the final report as something the user must audit.

"Mandatory grill row" (Part C) means **never resolved silently** — asked when
a human is there, and otherwise carried in the plan with an explicit resolution
mode and its evidence line. It does not mean "always starred": starring an
`evidence-forced` or `pre-set` row is noise that trains the user to skim past
the stars that matter.

## Part A — Feasibility triage (evidence)

Booley can drive four flows: **`sim`**, **`lint`**, **`synth`**,
and **`fpga`**. Feasibility is per-flow: a project may be a perfect fit
for simulate and lint while its synthesis flow needs extra wiring. Per flow the
verdict falls into one of three buckets. The authoritative EDA-tool → bucket mapping
lives in the supported EDA tools matrix, `docs/SUPPORTED-EDA-TOOLS.md` — treat it as the
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
  Verilator or Icarus (sim and lint), or Yosys + OpenROAD/OpenSTA (ASIC
  synthesis), or host-provisioned Vivado 2025.2 on Linux x86-64 when an
  administrator has already registered and granted the installation and no
  unvalidated floating-license behavior is required. Fastest path.
- **Yellow:** feasible, but something the plan **cannot fully settle through
  the approved runtime policy** is still in the way. Three shapes: an external
  dependency or experimental gate (a missing Vivado registration/Grant, or a
  required floating FlexNet checkout whose real paid-site behavior has not been
  validated); an
  input the repo **does not ship and somebody must author** (an SDC for a synth
  Target, an XDC for FPGA, a flat-port wrapper, a pass/fail sentinel a directed
  TB never prints); or a mechanical conversion whose input you have not actually
  read yet (a `.fl` filelist, a legacy EDA-tool-API `.core`).
- **Red:** out of reach today. A simulator outside the built-in matrix
  (Questa/ModelSim, VCS today), VHDL-only RTL against the built-in Verilog
  engines, encrypted RTL with no licensed simulator, or a license daemon that
  is unreachable for good (not just down for a restart). Widening the matrix
  is a Booley extension, not a project setup task.

**Calibrate on unresolved risk, not on config volume.** The verdict is what
warns the reader where this setup can still go wrong, so a step you have
*measured* stays Green no matter how much config it needs — a
`pre_run_commands` vector/firmware build with exact command lines and a timed
Session Runtime run (e.g. "17 s, 3.3 GB, `tests/generate.sh`") is planned work,
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
- `.booley_project/.gitignore`, a `FUSESOC_IGNORE` marker, `hooks/` holding
  five vendored scripts, and empty `tickets/{board/*,logs,locks}/` dirs;
- an **inner git repo** at `.booley_project/.git`, with no commit in it — but
  only when `[stealth] enabled` is on (the runtime fallback before setup makes
  its explicit choice); with the scrub explicitly off, init skips it and the
  dir is versioned nowhere;
- `.git/hooks/commit-msg` + `pre-push` delegators, and
  `.devcontainer/devcontainer.json`;
- one generated block in `.git/info/exclude` under the header
  `# Booley (generated; local, uncommitted)`: `/.devcontainer`,
  `/.booley_project`, `/.claude`, and `/.booley-projected-*.core`. Older init
  runs may have repeated the header; current init consolidates it.

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
  dogfood repo it is usually the **current** port's report, hours old, with
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
  configs) maps onto the supported EDA tools matrix (`docs/SUPPORTED-EDA-TOOLS.md`, read
  per the version rule in Part A's preamble). Verilator, Icarus, Yosys
  (+OpenROAD/OpenSTA) and sv2v are built in. AMD Vivado 2025.2 is built in only
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
  via `$fwrite` (Ibex does) needs a small stdout tee before it can score. And
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
    cannot be elaborated standalone, so both flows are dead until you add a thin
    **flat-port wrapper** (one small file, not a blocker; `booley doctor` flags
    it at setup time). `sim` is unaffected: a cocotb testbench brings its
    own wrapper. Symptom and wrapper recipe: Booley's `docs/TROUBLESHOOTING.md` ("interface
    parameter mismatch").
  - **Packed-struct / user-typedef ports** (`input fp_operation_type op`, a
    `typedef struct packed` or an enum from a package) — **no wrapper needed.**
    A packed struct is just a bit vector with names on it; the frontends flatten
    it. Plan the Target straight onto the real toplevel. What this shape *does*
    deserve is proof, because it is a known frontend-gap poker (the ravenoc/taxi
    shape): add an **execution-time check** that the frontend actually elaborates
    the struct-ported toplevel — the `synth` Target's own frontend is
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
  `-march` strings are a classic trap (Booley's `docs/TROUBLESHOOTING.md`, "RISC-V
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
- **Git submodules.** Run `git submodule status`. If the repo has any, add a
  decision row — but a short one: ticket worktrees get their submodules
  **copied out of the main repo**, never cloned, so nothing here is offline- or
  SSH-broken (mechanics in CONFIG.md → "Submodules"). What the plan owes is the
  precondition and, if the repo has heavy submodules nothing builds against,
  the explicit list:
  - Every submodule must be **present and clean in the main repo** before any
    ticket runs — one host-side `git submodule update --init --recursive` at
    setup, and no uncommitted work left inside a submodule. Worktree setup hard-
    errors otherwise, so recognize the two lines: `submodule <path> not found in
    main repo — run 'git submodule update --init' first` and
    `submodule <path> is dirty in main repo — commit or stash changes first`.
  - `.gitmodules` discovery is the default and needs no config. To copy only
    some of them, set `[submodules].paths` in `booley.toml` — it *replaces*
    discovery, so anything omitted is simply absent from ticket worktrees.
  - Execution-time check: a ticket worktree comes up with the submodule
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
  foundry PDK (rather than the reference Nangate45 flow) is outside the built-in
  flow (red for that flow).
- **License reachability.** A licensed-EDA-tool Flow is only real if the
  administrator can register an approved License Profile and its fixed server
  answers. The Session Runtime must receive licensing only through Booley's
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
synth  Yellow   image                              missing SDC to author
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
something only the user can supply (a license host, a host EDA-tool install, a
constraint value nobody can derive) and all Red flows. Star row 1 `review`
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
   it drives. Every enabled Flow executes inside the Session Runtime. Record
   any approved commercial provisioning separately in row 14.
2. **`.core` ownership/placement strategy & Target set** — decide this together
   with row 16's git footprint. The placement is deterministic:
   - **Open footprint + native `.core` exists:** reuse the appropriate native
     core. Modernize the selected legacy Target in place or add the needed
     modern Target to that core; do not create a parallel Booley core. Preserve
     unrelated legacy Targets unless the plan explicitly puts them in scope.
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
   `sim` / `lint` / `synth` / `fpga` (the Booley Flow family — `sim` also covers
   `elab`). The axis is not derivable from
   `.core` metadata, so the name must carry it. Reject plausible-but-wrong
   names now rather than at the Step-4 doctor NOTE: `asic_core` is wrong
   (`asic` is a legacy word, not an axis — use `synth_core`);
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
   firmware compile, vector staging)? — always a grill question. If yes, the
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
10. **Constraints** — ASIC synth needs an SDC per synth Target (hard error
    without one; `--default-clock` is the explicit opt-out), FPGA needs an XDC
    fileset. Does the repo ship them, or must they be authored?
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
12. **`elab`** — expose it (needs a resolvable Target) or opt out with
    `enabled = false`.
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
16. **Git footprint — hidden, open, or an explicitly requested hybrid?** Whether `.booley_project/` is visible
    in the RTL repo's tracked tree. **Always a grill question**: the codebase
    cannot answer whether the user's colleagues are meant to know. *Hidden* (the
    default, and what `booley init` already set up): `.booley_project/` stays
    **untracked** in the RTL repo, excluded through the parent repo's
    `.git/info/exclude` — never `.gitignore`, which is itself a tracked file and
    would advertise Booley in the history it is supposed to keep clean. *Open*:
    `.booley_project/` is **committed** to the RTL repo like any other project
    config. **Hybrid** is reserved for an explicit port/integration policy:
    keep `.booley_project/` hidden, but track the named native cores,
    constraints, wrappers, durable root `AGENTS.md`, and report required by
    that policy. Record the exact tracked allowlist in this row; do not broaden
    it into committing operational state. Step 4 executes whichever this row says.
    A hidden authored `.core` is the stealth layout: row 20 must enable stealth,
    which also activates ignored root-level core projection. An open project
    uses tracked native cores. A hidden config-only project may leave stealth
    off, but it cannot author cores under `.booley_project/cores/`.
    When native `.core` files exist, ask exactly: **"Should Booley ignore the
    repository's existing `.core` files and use only the stealth-authored
    cores?"** Record `ignore_native_cores = true` only from an explicit yes.
    Recommend yes when evidence shows the native cores fail the installed
    FuseSoC schema or cannot express the selected Flow Targets; otherwise
    recommend no. Explain that the switch affects Booley resolution, not raw
    `fusesoc --cores-root <repo>` commands.
17. **Specialists** — beyond the core Flows (rows 1, 12) and style lint
    (row 11), does the user want any Booley Specialist explicitly disabled
    from the start — `reviewer`, `mutation_tester`, or another? Every installed
    Specialist is discovered automatically; Step 2 writes
    `[mcp_tools.<name>].enabled = false` only for an intentional opt-out. One caveat
    before enabling: **`mutation_tester`
    has not supported cocotb-based sim Targets** — its baseline runner drives a
    `V<toplevel>` binary, and a Cocotb Target builds `Vtop` driven from Python
    over VPI. Verify current support before enabling it on a cocotb project;
    unsupported, it burns a full specialist run to report an infra error.
18. **Parity check (optional)** — whether to validate Booley's results against
    the repo's **native build system** in an optional Step 5, and with which
    oracle. **A grill question**: the codebase cannot say whether the
    self-checking TB is a strong enough correctness oracle on its own, or
    whether the user wants the old flow captured as a cross-check. Gated on
    **EDA tool identity** — parity is comparable only per phase where Booley's
    selected EDA tool equals the native flow's EDA tool (native VCS vs Booley Verilator
    → not comparable, `none`). The first cut compares `sim` only (verdict +
    cheap telemetry). Default `none`; see `steps/5-parity.md`.
    **Symmetric case: when the EDA tools are *identical*, do not default to
    `none`.** A repo whose native sim script runs the same engine Booley
    selected (Verilator ↔ Verilator, Icarus ↔ Icarus, same design, same TB) is
    handing you a free exact oracle — same EDA tool, same sources, so any verdict
    difference is Booley's wiring and nothing else, and Step 5 is minutes of
    work post-gate. Propose `sim` parity with the native script named as
    evidence. Unattended fallback for this case is **`sim`, not `none`**;
    `none` stays the fallback only where the EDA tools differ or the repo has no
    runnable native flow. It is post-gate and never blocks completion, so the
    downside of a wrong `yes` is one skipped optional step.
19. **Agent backend (provider)** — which model provider the developer and every
    specialist run on: `claude` (Anthropic) or `codex` (OpenAI GPT-5.x).
    **Always a grill question, never inferred** — the codebase cannot say which
    subscription the user intends to bill, and the two authenticate through
    different credentials (Anthropic OAuth / API key vs `~/.codex/auth.json`);
    picking silently can bill the wrong account. Writes `[agent] provider`
    (+ `auth`, e.g. `subscription` to bill an OAuth login rather than an API
    key) in `booley.toml`. Booley's own default when `[agent]` is omitted is
    `claude`, but do not lean on it: ask, and only fall back to `claude` when
    the user explicitly declines to choose.
20. **Stealth mode (`[stealth]`)** — coupled to row 16 when hidden cores are
    authored. **Disabled
    by default during setup; enabling it requires an explicit yes.** When on, a
    commit-msg hook redacts a banned-word list (`claude`, `anthropic`, `codex`,
    `booley`, …) out of every commit message in the RTL repo, and can also cap
    the body (`max_body_lines`) or allowlist author identities
    (`allowed_authors`). **Never evidence-forced** — no codebase signal says
    whether the user wants their history scrubbed. Interactive: ask exactly,
    **"Do you want stealth mode: self-contained hidden cores plus the
    commit-message scrub?"** Recommend `no` unless row 16 chose a hidden core,
    explain both effects, and resolve `enabled = true` only from an affirmative
    answer or that hidden-core choice; otherwise resolve `enabled = false`.
    It also keeps authored cores self-contained under `.booley_project/cores/`
    and projects ignored copies into the RTL root for FuseSoC. Unattended:
    write `enabled = false` unless row 16 requires a stealth core layout. A
    repo that already carries a hand-set
    `[stealth]` block is the exception: apply the prior-footprint rule, resolve
    `pre-set`, and keep its value unchanged.
    `booley init` creates
    `.booley_project/`'s own inner git repo *only* while `[stealth] enabled` is
    on. With the scrub off, a hidden-footprint project dir is versioned nowhere
    until someone `git init`s it — flag that for Step 4's footprint work rather
    than letting the combination pass silently.
21. **Feedback mode (`[feedback] mode`)** — whether Step 6 may *offer* to send
    the run's Booley-side findings upstream, and to where. Asked here, at
    planning time, so the answer is on record before anyone is tired at the end
    of a long setup — and so Step 6 never has to guess. **Never
    evidence-forced**: no codebase signal says what a team's disclosure rules
    are. Four values:
    - `ask` (**default**) — Step 6 shows the transient redacted view and asks once, for
      a **public** issue on Booley's tracker. A decline is final for the run.
    - `email` — the same offer, the same preview, but the destination is a
      private mail to Booley's maintainer (`boldaxolotl@proton.me`) instead of a
      public issue. Booley builds a `mailto:` link; the user's own client sends
      it. No GitHub account needed, and nothing is published.
    - `file-only` — Step 6 never offers to send anything. The user may later run
      `booley feedback export` if they want a redacted file to route through
      their own review.
    - `off` — Step 6 writes only the local report and stays quiet.
    **Ask it neutrally and do not sell it.** Say what it is (an optional bug
    report that helps Booley get fixed), and say the two things that decide it:
    where it lands and under whose name (public issue + **GitHub account name**,
    or a maintainer's inbox + their **email return address**), and that
    redaction is a best-effort denylist over paths, remotes, and design
    identifiers — not a guarantee. Whatever they pick, they see the exact text
    before anything is sent. Unattended: `file-only`, never `ask` or `email` — an
    unattended run has nobody to read a preview, and the offer is not one an
    agent may accept on a user's behalf.

Then add the **repo-specific rows**, numbering on from 22 — everything Part A
surfaced that the standard list doesn't name: generator steps, **git
submodules** (a row whenever `git submodule status` is non-empty: the host-side
init/clean precondition, and `[submodules].paths` if only some should reach
ticket worktrees), **scope exclusions** (a VHDL twin, a subsystem nobody
targets — say what is excluded and why, never leave it implied), multi-clock
timing intent, environment modules, a TB stdout tee, unusual directory layouts.
The checklist is the floor, not the ceiling.

Close with the **execution-time checks** list: every planned verification that
needs the sandbox (fusesoc target resolution, `-march` compile check, ingest
smoke, image EDA-tool probes, submodule population in a ticket worktree). Steps
2–4 run these; a failed check that contradicts a decision triggers the
deviation rule.

## Part C — The grill (interactive mode only)

This is the skill's most user-facing moment, so the **onboarding voice**
(SKILL.md) is in full force: assume the user is new to Booley. Every question
carries its recommended answer and a plain-English reason it matters, and any
Booley term gets defined the first time it appears — a user who does not yet
speak Booley still has to make every call here.

Refine the decision sheet with the user, ticket-creation style:

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
- **The mandatory rows are non-negotiable.** Rows 4 (TB flavor), 16 (git
  footprint), 17 (specialists), 18 (parity), 19 (agent backend), 20
  (commit-message scrub), and 21 (feedback mode) are marked *always a grill
  question* — none may be
  silently defaulted, even for a clean three-question repo. Agent backend in
  particular has no codebase signal at all, so it is easy to skip by reflex; ask
  it every time. Two exceptions, both from "How a row resolves":
  **evidence-forced** (rows 4 and 18 can be settled by the repo — then you
  *state* them with their evidence: "all 8 test modules are cocotb, so the
  flavor is cocotb" is a confirmation line, not a question), and **`pre-set`**
  (the value is already hand-set on disk — confirm it in one line, don't
  re-litigate it). Rows 16, 17, 19, 20, and 21 are never evidence-forced.
- **Codebase first**: never ask what the repo can answer. Ask to *confirm*
  low-confidence inferences, to *choose* where evidence genuinely
  under-determines (TB flavor, the Target set / config variants, style lint,
  host-EDA-tool placement), and to
  *supply* what only the user knows (license servers, which flows they care
  about, host EDA-tool installs).
- **Proportional depth**: a clean single-core Verilator repo needs three
  questions; a 400K-LOC multi-core repo with firmware deserves a real session.

Format every question like this:

```md
❓ **Q1** - **<question title>**: <question body, including choices when useful>

➡️ <recommended answer, the supporting evidence, and why>
```

Stop only when the frontier is empty and no row remains silently assumed:
every row is `evidence-forced`, `pre-set`, `user-confirmed`, or `inferred` at
high confidence. Summarize the resulting shared understanding and ask the user
to confirm it. Do not write the plan or move to Part D before that confirmation.

## Part D — Write the plan and get approval

Fill `../SETUP_PLAN_TEMPLATE.md` and write it to
`.booley_project/SETUP-PLAN.md`:

- **§1 Feasibility** — the per-flow verdict table + determinant evidence.
- **§2 Decision sheet** — the finished table, including repo-specific rows and
  the execution-time checks list.
- **§3 Approval & deviations** — the approval record; the deviation log starts
  empty and is appended by execution steps.

**Interactive:** show the user the complete plan and ask for exactly one
action: `approve`, `edit`, or `cancel` (default `cancel` on ambiguity). On
`approve`, set `status: approved` and continue to execution.
**Unattended:** set `status: auto-approved`, leave the `review`-flagged rows in
place, and continue. Do not stall. The approval line records the contract:
auto-approved, `N` rows starred `review` for the user to audit, the rest
`evidence-forced`, `pre-set`, or `inferred`. If *every* mandatory row came back
`review`, say
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
   `.hex`. A `post-setup` hook runs inside the container after each ticket
   worktree is created; point it at the repo's software build (Ibex's is a
   Make target):

   ```bash
   # .booley_project/hooks/post-setup.sh — the riscv toolchain is on PATH
   make -C <path/to/sw-build-dir> ARCH=rv32imc_zicsr   # e.g. Ibex's coremark make target
   ```

   The explicit `_zicsr` is not cosmetic — the sandbox GCC won't assemble an
   older project's default `-march=rv32imc` without it. See Booley's
   `docs/TROUBLESHOOTING.md` ("RISC-V firmware won't assemble against the sandbox GCC")
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
