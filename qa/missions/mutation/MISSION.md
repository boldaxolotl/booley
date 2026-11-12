# Mutation Flow on a fixture with known counts

Hunt Mutation Flow bugs end to end: which Mutants the generator plants, how it
picks them, how it scores them, and how it reports hangs, invalid Mutants,
rejected input, and removed surface. The areas run on a small fixture Project
whose every Mutant site is listed by hand, so each mismatch is a finding
rather than a judgment call. The `booley-qa-run` skill runs this mission;
skim `../../shared/mutation/RUNBOOK.md` before area 1.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Fixture Project: `../../shared/mutation/project/` in this repository, with
  the hand-written oracle beside it in `../../shared/mutation/expected.json`.
- Tools/host needs: the issued Sandbox with Icarus, Verilator and cocotb; no
  agent CLI credentials are needed. The Flow uses no model.
- Budget: 8 h total: 375 area minutes plus 105 contingency. Areas run in
  priority order; log areas not reached as skipped. Run areas sequentially.
  Rerun on Windows only when asked.

## Staging the fixture
Follow RUNBOOK.md "Staging": a fresh disposable Project (add it to
`resources.md`), initialized through the public route, `tests.toml` installed
in the resolved Booley Directory, fixture and initialized files committed.
Run both baselines first; a failing baseline is a finding, and the closest
manual state replaces the skipped area.

## Mission-specific rules
- Fixture bytes, the registry and `expected.json` are fixed. A mismatch
  between a run and the oracle is a finding, or a `qa-bug` when the oracle's
  derivation (its `rule`) is wrong. Never edit the oracle to match a run.
- Identify Mutants by `file:line:site->replacement`; IDs are hashes. Read
  them from `mutation-specs.md` and the manifest.
- Every fault works on a run-owned copy; save the failed output, then restore
  from RUNBOOK.md "Fault files".
- Run `sim_icarus` by default. Run the one exhaustive `sim_cocotb` run in area
  2; other areas use `sim_icarus` or a `--scope` subset on `sim_cocotb`, because
  each Verilator Mutant builds and costs far more.
- Follow the doc, not the code. When the report and `docs/user/FLOW-MUTATION.md`
  disagree, that is the finding; say which one you believe.

## Areas

### 1. mut-determinism — Same inputs, same Mutants (~55 min)
Intent: the RTL, scope, `--count` and `--seed` alone decide the Mutants.
Try:
- Repeat `booley flow mutation --target sim_icarus --count 10 --seed 1 --dry-run`
  and a real run; compare Mutants, `mutation-specs.md` and the manifest identity
  (scope hashes, generator and pyslang versions, seed, count).
- Same seed on `sim_cocotb`: the Mutants equal the `sim_icarus` ones (same
  scope). Another `--seed` changes the pick; `--seed` rejects a negative or
  non-integer value.
- Add a comment line in `rtl/counter.sv`: other files keep their IDs and
  relative pick order. Add a comment line above `alu` in `rtl/alu.sv`: the
  IDs of `alu_shift`'s Mutants, in the same file, stay.
Look for: IDs that move with unrelated edits, a seed that does nothing,
identity fields missing from the manifest.

### 2. mut-sites — Every site, no decoy (~70 min)
Intent: an exhaustive run plants exactly the oracle's candidates.
Try:
- `--target sim_icarus --count 41`; compare `mutation-specs.md` with
  `operators` in `expected.json`: per operator, the same file, line, site and
  replacement, nothing missing, nothing extra. Run `--target sim_cocotb --count 41`
  once and compare the same way.
- No `decoys` entry is mutated, including the `ifdef` branch, the string, the
  attribute, widths, parameter defaults, and the boundary selects.
- Every Mutant applies: `campaign/variants/` holds each one; lint each deletion
  variant with the Target's simulator in the Sandbox (still parses).
Look for: a missing or extra site, a decoy mutated, a Mutant that does not
match the report text, deletion text that no longer parses.

### 3. mut-selection — Picking and `--count auto` (~35 min)
Intent: selection follows the documented round-robin and sizing.
Try:
- `--count 6` takes one Mutant per operator in `operator_order`; `--count 7`
  adds a second `relational`; `--count 18` and `--count 22` match
  `picks_per_operator`. Which candidate is seed-dependent and not checked.
- `--count auto --dry-run` at the default timeout and with `--timeout 10m`,
  `1m`, `1h`: compare with the `auto` cases, and check that the dry run
  explains the count it chose.
- `--count 50`: all 41 run, the shortfall line matches `selection.shortfall`.
Look for: a wrong operator order, an `auto` count off the oracle, a silent
shortfall.

### 4. mut-scoring — Tally, threshold and verdict (~50 min)
Intent: the verdict follows detected counts and the threshold rule.
Try:
- Exhaustive `sim_icarus` run: the tally equals `totals.exhaustive`,
  survivors are listed as "not detected" with file, line and code, and equal
  `outcomes.survivors`. The default threshold is `ceil(0.8 x count)`; compare
  `default_threshold_by_count` for other counts.
- `--min-detected 33` passes and `--min-detected 34` fails (`totals.boundary`);
  exit codes 0, 1, 2.
- The equivalent-Mutant note appears only when survivors exist: `--scope
  rtl/counter.sv` shows it, `--scope rtl/alu.sv --count 14` shows none.
- The criterion detail `mutation_score_<target>` matches the report.
Look for: a threshold on the wrong base, invalid Mutants counted as detected,
the equivalent note on a run without survivors, an exit code off the verdict
table.

### 5. mut-timeouts — The hanging Mutant (~40 min)
Intent: a Mutant that never finishes counts as detected and says why.
Try:
- `--scope rtl/handshake.sv --count 1` on `sim_icarus` and `sim_cocotb`: the
  Mutant is `detected (timeout)`, counted apart from ordinary detections and
  included in detected.
- Read the limit from the baseline durations: `min(300, max(30, ceil(10 x
  baseline)))` per test; the 30 s floor applies. A limit that no log records
  is an observability finding. Check the 300 s cap by reading unless time
  allows a full wait.
Look for: a hang that stalls the whole run, a timeout counted as not detected
or as an error, a limit that ignores the baseline.

### 6. mut-scope — What `--scope` accepts (~35 min)
Intent: the default scope is the Target's own RTL and `--scope` only narrows it.
Try:
- The default scope equals `default_scope`: no testbench, vendored or generated
  file; `--scope` narrows it.
- Reject before any build: `tb/mutation_tb.sv`, `rtl/vendor/sync2.sv`, a
  missing path, a `../` path, a non-HDL file (`tb/test_mutation.py`). Check
  each exit code against the doc's verdict table: an unusable `--scope` entry
  is `ERROR`, exit 2.
Look for: a rejected entry that still builds, a wrong exit code, a vendored or
generated file in the default scope.

### 7. mut-failures — Baselines and bad input (~50 min)
Intent: no verdict is invented when the run cannot give one.
Try:
- `faults/tests-baseline-fail.toml`: `ERROR`, exit 2, no Mutant run.
- `faults/unelaborated.sv`: baseline does not elaborate, `ERROR`, exit 2.
- `faults/syntax_error.sv`: the run names the file and the parser diagnostic,
  exit 2.
- The `self_check.sv` Mutants (`scope rtl/self_check.sv --count 3`) are
  `invalid`, unscored, and still counted in `--count`. Do this on both Targets.
- A run with no agent CLI credentials in the Sandbox works.
Look for: one inconclusive Mutant (a cocotb run with no result line, a compile
failure outside the build step) turning a whole run into `ERROR`, a Mutant
that fails to build counted as detected.

### 8. mut-removed — The old Specialist surface is gone (~25 min)
Intent: removed names fail clearly and leave nothing behind.
Try:
- `booley flow mutation --steer x` and `--regen-lock` are rejected by the
  argument parser.
- `booley specialist mutation_tester` is refused with `Use booley flow mutation.`.
- `[models] mutation_tester` and `[specialists.mutation_tester]` in the
  Project config are each reported with their fix hint.
- A leftover lock file and a `mutation_tester` scratch directory in the
  Booley Directory are ignored, and the scratch directory is removed.
Look for: a removed flag that is silently accepted, a hint that names a surface
that no longer exists.

### 9. cleanup — Mutation cleanup (~15 min)
Reap simulators, restore fault files and the registry, and release the fixture
Project and its resources in `resources.md`.

## Known traps
- Mutant IDs are hashes; compare by file, line, operator, site and replacement.
- A hanging Mutant costs at least 30 s per test; a full exhaustive Verilator
  run is slow, so run it once.
- cocotb runs the suite in batches, so one bad test can touch its neighbours.
- Plain `booley flow sim` reports a failed Icarus build as `ERROR` but a failed
  Verilator build as `FAIL`; the Mutation Flow should call both `invalid`.
- Parallel sub-agents share the host Sandbox cap (`[sandbox] max_sessions`,
  default 4); new Sandbox starts are refused at capacity.
