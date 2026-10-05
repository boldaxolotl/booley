---
status: accepted
---

# Declare Test Variants and seeds instead of run-time overrides

Engineers need to run a Test with other plusargs or another seed (issue #1233).
A pass only means something for the exact configuration that ran, so Booley
offers no free-form run-time overrides. A Project declares each extra
configuration as a **Test Variant** in `tests.toml`, and every **Test Run** on
a Target that can apply seeds has an explicit, recorded seed. Simulation evidence counts only when a Test Run
matches a declared configuration exactly, so a pass under ad-hoc arguments can
never stand in for the declared Test. Implementation waits for Goal Mode
(ADR 0067), which these Goal rules extend.

## Decision

- **Test** keeps its current meaning: a name in a Target's `tests.toml` list
  that the testbench recognizes, sent through `select` (or the cocotb filter).
- **Test Variants** belong to one Test and add a fixed list of raw run-time
  arguments, plus environment variables. A Cocotb Target's Variants carry
  environment only. When a Test has Variants, the Project must name one as its
  default:

  ```toml
  [sim]
  select = "+test_id={name}"
  tests = ["reset", "basic", "stress"]

  [sim.variants.stress]
  default = "short"
  short = ["+iters=1000"]
  soak  = ["+iters=100000"]
  fault = { args = ["+err_rate=5"], env = { FAULT_MODE = "1" } }
  ```

  A Variant can have an empty argument list. This is the migration path for a
  Test that gains Variants but keeps its old behaviour as the default.
- **Names.** A Variant is written `test+variant`, and a Test Run is written
  `test+variant@seed`. A Test without Variants has no Variant part
  (`test@seed`). The bare Test name means its default Variant, and running a
  Test runs only its default Variant. Running a Target runs the default
  Variant of each Test; other Variants run only when named. Test and Variant
  names cannot contain `+` or `@`, and `default` is reserved.
- **Arguments stay run-time.** Variant arguments go to the simulation command
  after the `select` argument and add no build inputs. A Variant cannot
  repeat the `select` or seed option, and cannot override a plusarg that the
  Target declares: the Target owns its parameter values. Variant environment
  cannot set variables that Booley or the simulator integration owns, such as
  the cocotb module, filter and seed variables or any `BOOLEY_*` name.
- **One process per configuration.** Test Runs that differ in arguments,
  environment or seed never share a simulator process. A cocotb batch holds
  only Test Runs with the same effective environment and seed.
- **Pre-Sim Commands** receive the resolved Test Run names, so a hook can
  stage per-Variant inputs such as firmware.
- **Seeds.** Booley never relies on a simulator's own default seed. A Test
  Run that names no seed uses the **Default Seed**: a Booley-wide constant
  that a Target can override in `tests.toml`. Booley uses the simulator's
  native seed option where one exists. For cocotb this is the seed variable
  of the installed cocotb version (`COCOTB_RANDOM_SEED` on 2.x, `RANDOM_SEED`
  on 1.x). Other simulators, and testbenches that seed themselves, use a `seed`
  argument template in `tests.toml`, like `select`. A Target with neither
  mechanism has unseeded Test Runs: they are written without `@seed`, record
  no seed, and Booley refuses any seed for them. Each seed mapping is proven
  by a test in which the same seed repeats the random values and a different
  seed changes them. `tests.toml` declares no seed lists.
- **Random seeds.** For `--seed random`, Booley picks a concrete seed before
  it publishes the Simulation Campaign manifest. The seed is part of each
  work item's identity, so resume reuses it.
- **Reporting.** Every Test Run records its resolved name, seed, arguments and
  environment. A failure prints the command to rerun it with the same seed.
- **Goals.** A simulation Goal names `test`, `test+variant`, or
  `test+variant@seed`. A Goal without a seed requires the Default Seed, and a
  Goal with a seed requires that exact seed. At Goal Mode entry, Booley
  resolves each Goal to its full Test Run form and freezes it. Later
  `tests.toml` edits to defaults therefore cannot change what a Goal means.
  A Goal whose resolved Test Run no longer exists stays unmet until a human
  approves a change. A Goal that names a Target or Test that does not exist
  yet (ADR 0067) resolves and freezes when that Test first exists, and the
  review package shows the resolution. An agent that adds a seeded Goal is
  changing Goals, so it needs human approval.
- **Multi-seed Goals.** A Goal written `test+variant@xN` requires N Test Runs
  of that Variant, each with a different seed, and all N must pass. N must be
  at least 1 and at most a Booley-wide limit. When Booley resolves the Goal,
  it draws N distinct seeds at random, without replacement, from a range that
  every supported seed mechanism accepts. It then freezes them with the Goal.
  Every Goal Mode session therefore samples new seeds, but reruns within a
  session can't sample new ones. Seed ranges are not offered: a reproduction
  names the one failing seed. A Target that cannot apply seeds refuses this
  form. Adding a multi-seed Goal or changing its N is a Goal change.
- **Combining multi-seed Goals.** Goals are canonicalized (Target identity,
  resolved Test Variant, seed form) and deduplicated before any seeds are
  drawn. When two multi-seed Goals name the same Target and Variant, the one
  with the larger N wins, following ADR 0067's stricter-Goal rule.
- **Running a multi-seed Goal.** Inside Goal Mode, `--test test+variant@xN`
  runs the frozen seeds of the Goal with the same Target, Variant and N. With
  no such Goal, it runs N new seeds as a diagnostic run and says so. Outside
  Goal Mode it always draws N new seeds before it publishes the Simulation
  Campaign manifest. `--seed` together with an `@` form in `--test` is refused
  rather than letting one override the other; the MCP tool validates the
  same rule. One request can span more than one Simulation Campaign, which
  keeps the campaign work-item limit.
- **Multi-seed evidence, per seed.** A frozen seed counts as passed when it
  has a passing Test Run at the current dependent file contents (ADR 0067)
  and no failing Test Run at those contents. A failure at the same contents
  is never cancelled by a later pass, so a flaky seed stays failed until the
  code changes. Seeds can pass in separate runs and Simulation Campaigns,
  including after an interruption. The Goal is met when every frozen seed has
  passed. The report lists every failing seed with its rerun command.
- **Evidence.** A Test Run is evidence for a Goal only when its resolved name
  and its fingerprint of declared arguments and environment match the Goal
  exactly. A failure of a matching Test Run is never cancelled by a later pass
  at the same dependent file contents. Every other Test Run is diagnostic. `--seed N|random` and naming a
  non-default Variant are both normal Simulation Flow inputs; they produce
  evidence only by matching a Goal. A failing diagnostic Test Run is
  reported prominently but does not block a Goal.
- **Surface.** `flow sim` gains `--seed N|random`, and `--test` accepts the
  `test+variant` form. The sim MCP tool exposes the same inputs.

## Considered Options

- **Free-form `--plusarg`** (the issue's proposal) was rejected for v1. An
  undeclared argument can redefine what a Test checks (for example
  `+iters=10` or `+disable_scoreboard`), and agents could invent a passing
  configuration. A one-off `+verbose` therefore also needs a declared Variant.
- **`--param NAME=VALUE` over Target-declared parameters** was rejected.
  The Target owns its parameter values with no per-call override surface, and
  `vlogdefine`/`vlogparam` overrides would also change the build.
- **`--define`** stays retired, for the same reason.
- **Test entries that name their testbench test** (dvsim `uvm_test`,
  riscv-dv `rtl_test`) were rejected. They change what a "Test" means for
  every existing Project, and a design engineer reads "bench test" as jargon.
  A Variant leaves Test unchanged and is purely additive.
- **Seed lists or seed counts in `tests.toml`** were rejected. How many seeds
  a pass requires is a property of the Goal, not of the Test, so multi-seed
  evidence is declared as a Goal (`@xN`).
- **Fresh random seeds on every evaluation of a multi-seed Goal** were
  rejected. An agent could rerun until a rare failure missed the sample.
  **A fixed seed range** (`@1..1000`) was rejected because it repeats the
  same seeds in every session and stops exploring.
- **Blocking a Goal on any failing seed** was rejected. An agent could then
  get stuck on an unrelated bug it found by chance. A human promotes such a
  seed into a Goal.

## Consequences

- `tests.toml` gains the `variants` tables and an optional per-Target default
  seed and `seed` template. Existing `tests` lists keep working unchanged.
- Variants and seeds add no build inputs, so they never force a new
  Simulator Bundle by themselves. Bundle sharing still follows the existing
  Pre-Sim build-access rules (ADR 0064).
- Cocotb Targets whose Tests differ in environment or seed run in more than
  one simulator process.
- An agent can read the frozen seeds and could special-case them in the
  testbench. This design does not prevent that mechanically: testbench
  changes are visible in the review package, as for any other attempt to game
  a Goal.
- The fingerprint covers declared inputs only. Environment variables that the
  Sandbox passes on to the simulator are outside it, as they are today.
- Test names are not validated today. Validation becomes required, both in
  configuration loading and in Doctor.
- Changing the Booley-wide Default Seed changes every resolved Test Run
  without a seed, so it needs a release note.
