# Declare Test Variants and seeds instead of run-time overrides

Status: accepted

## Context

A pass only means something for the exact configuration that ran, so Booley
offers no free-form run-time overrides. A Project declares each extra
configuration as a **Test Variant** in `tests.toml`, and every **Test Run** has
an explicit, recorded seed where the Target can apply one. Evidence counts only
when a Test Run matches a declared configuration exactly.

[ADR 0092](0092-measure-commits-against-target-metrics.md) removes the Goal
rules below together with Goal Mode. Test Variants, seeds and Test Runs stay.

## Decision

### Identity

- A **Test** is a name in a Target's `tests.toml` that the testbench
  recognizes. A **Test Variant** belongs to one Test and adds a fixed list of
  run-time arguments plus environment variables; a Cocotb Variant carries
  environment only. A Test with Variants must name one as its default. An
  empty argument list lets a Test gain Variants and keep its old behaviour.
- A Test Run is written `test+variant@seed`; a Test without Variants has no
  Variant part. The bare Test name means its default Variant, and running a
  Test or a Target runs only default Variants; others run only when named.
  Test and Variant names cannot contain `+` or `@`, and `default` is reserved.

### What a Variant may do

- Arguments stay run-time: they follow the `select` argument and add no build
  input, so a Variant or seed never forces a new Simulator Bundle. A Variant
  cannot repeat the `select` or seed option, or override a plusarg the Target
  declares: the Target owns its parameter values. Variant environment cannot
  set variables Booley or the simulator integration owns (the cocotb module,
  filter and seed variables, any `BOOLEY_*` name).
- Test Runs that differ in arguments, environment or seed never share a
  simulator process; a cocotb batch holds only Test Runs with the same
  environment and seed.
- Pre-Sim Commands receive the resolved Test Run names to stage per-Variant
  inputs; the bare-name variables keep meaning the Test, since hooks index
  inputs by Test.

### Seeds

- Booley never relies on a simulator's default seed. A Test Run that names no
  seed uses the **Default Seed**, a Booley-wide constant a Target may
  override in `tests.toml`. Changing the constant changes every unseeded
  resolved Test Run, so it needs a release note.
- Booley uses the simulator's native seed option where one exists (Verilator,
  cocotb's seed variable); others, and self-seeding testbenches, use a `seed`
  argument template. A
  Target with neither has unseeded Test Runs: no `@seed`, no recorded seed,
  and any seed is refused. `tests.toml` has no seed lists. Xcelium's native
  option
  waits for an Xcelium launch path.
- `--seed random` picks a concrete seed before the Simulation Record manifest
  is published and makes it part of the work item's identity, so resume
  reuses it. `--seed` together with an `@` selector is refused, by the CLI and
  the MCP tool alike. One request resolving to more than 1000 Test Runs of a
  Target is refused before planning, so a request is one Simulation Record
  per Target.
- A failure prints the command that reruns the Test Run with the same seed.

### Goals (end with Goal Mode)

- A Goal names `test`, `test+variant` or `test+variant@seed`. Goal Mode entry
  resolves it to full Test Run form and freezes it, so later `tests.toml`
  default edits cannot change its meaning. A Goal whose Test Run vanishes stays
  unmet until a human approves a change; one naming a Target or Test that does
  not exist yet freezes when it first exists. Adding or changing a seeded Goal
  is a Goal change.
- `test+variant@xN` requires N Test Runs with N distinct random seeds, drawn
  at resolution and frozen with the Goal: each session samples new seeds, reruns
  within it cannot. No seed ranges; a reproduction names the one failing seed.
  The larger N wins for the same Target and Variant.
- A Test Run is evidence only when its resolved name and its fingerprint of
  declared arguments and environment match the Goal exactly; every other run is
  diagnostic, reported but never blocking. A failure at the same dependent file
  contents is never cancelled by a later pass, so a flaky seed stays failed
  until the code changes.
- Exceptions: a `--diagnostic` run records nothing, even for frozen Test Runs,
  but names the Goals whose frozen runs ran or failed. A coverage Goal is
  decided by its coverage floors alone, from one run of exactly its frozen Test
  Runs, whether or not they passed.

## Rejected alternatives

- **Free-form `--plusarg`:** an undeclared argument can redefine what a Test
  checks (`+disable_scoreboard`), letting agents invent a passing
  configuration. A one-off `+verbose` needs a declared Variant too.
- **`--param`, `--define`:** the Target owns its parameter values; overriding
  them changes the build.
- **Test entries naming their testbench test:** they change what a Test means
  for every Project. A Variant is additive.
- **Seed lists in `tests.toml`:** seed count belongs to the Goal, not the Test.
- **Fresh seeds per evaluation:** an agent could rerun until a rare failure
  missed the sample. **A fixed seed range** never explores.
- **Blocking a Goal on any failing seed:** an agent could get stuck on a bug
  found by chance. A human promotes such a seed into a Goal.

## Consequences

- An agent can read frozen seeds and special-case them in the testbench. This
  is not prevented mechanically; testbench changes show in review.
