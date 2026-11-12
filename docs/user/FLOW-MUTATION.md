# Mutation testing (`mutation`)

`mutation` checks that your tests catch bugs. It plants small bugs (Mutants) in
the RTL, runs the Target's tests against each one, and reports how many Mutants
the tests detected. A passing simulation only shows the tests accept the
design; a high mutation score shows they would notice if it were wrong. No LLM
is involved: the same RTL, `--count`, and `--seed` always give the same
Mutants, so scores are reproducible and comparable across runs. For what all
Flows share (choosing Targets, common options, logs, exit codes), see
[Running Flows](FLOW_REFERENCE.md).

Mutation drives a [Simulation](FLOW-SIM.md) Target and runs the whole suite of
that Target's Tests, so the Tests must already pass.

## Running mutation testing

1. Check that the Project has a simulation Target (`booley targets --for sim`
   lists them). There is no `--for mutation`: mutation runs on any simulation
   Target.
2. Ask the Developer Agent:

    > *"Check whether the FIFO tests catch injected bugs."*
    >
    > *"Run mutation testing on `sim_fifo` with 20 Mutants."*

3. The agent reports how many Mutants were detected, which survived, and the
   path to the report.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow mutation --target sim_fifo --count 20 --seed 1
```

Mutation takes exactly one Target; `--target` is required and a list is
refused.

| Option | Effect |
|---|---|
| `--scope <file,...>` | RTL files to plant Mutants in. Default: the Target's own RTL, without testbench, vendored, and generated files. |
| `--count <N\|auto>` | Number of Mutants (default `10`). `auto` derives it from the size of the scope, capped by the time limit (about one Mutant per 150 seconds, at least 3). |
| `--seed <N>` | Non-negative integer that selects the Mutants (default `0`). Change it to try a different sample. |
| `--min-detected <N>` | Detected Mutants needed to pass. Default: 80 % of `--count`, rounded up. |
| `--dry-run` | Validate the Target and scope and print the count, threshold, and seed. Nothing is built or simulated. |

The shared options (`-C/--project`, `--timeout`, `--report-dir`, `--diagnostic`,
`-q/--quiet`) work as for every Flow.

## How a run works

1. Booley selects the Mutants for the seed from the scope files.
2. It builds the unmodified RTL and runs every test of the Target. All must
   pass; otherwise the run ends with `ERROR`, because a failing test could not
   tell a Mutant apart from the original.
3. Each Mutant is applied to a private copy of the sources, built, and the
   Target's tests run on it. Your RTL is never modified.
4. A Mutant is **detected** when a test fails on it, or when a test runs past
   its time limit (reported as `detected (timeout)`): ten times the test's
   baseline duration, at least 30 seconds and at most 300 seconds. A Mutant
   that no test fails on **survives**. A Mutant that does not compile is
   `invalid`: it is reported, not scored, and can never be detected, so it uses
   up part of the threshold's margin.

Every run rebuilds the baseline and every Mutant, so a result never depends on
stale builds. Some survivors may be equivalent Mutants that change no
observable behavior, which is why the default threshold is 80 %, not all.

## Which Mutants it plants

Booley parses each scope file (Verilog or SystemVerilog) and finds every spot
where one of six operators applies. Each operator makes one small change; a
spot with several possible changes yields one candidate Mutant per change.

| Operator | What it changes | Example |
|---|---|---|
| `relational` | Swaps a comparison for its neighbour. | `a < b` -> `a <= b`, `a == b` -> `a != b` |
| `arithmetic-logical` | Swaps an arithmetic, bitwise, logical, or shift operator. | `a + b` -> `a - b`, `a & b` -> `a \| b`, `a << 2` -> `a >> 2` |
| `condition` | Replaces the condition of an `if` or `?:` with its negation, `1'b1`, or `1'b0`. A `while`, `do`-`while`, or `for` condition is only forced to `1'b0`. | `if (valid)` -> `if (!(valid))` |
| `constant` | Changes a numeric literal's value: lowest bit flipped, plus one, or minus one. Size and base stay. | `4'd5` -> `4'd4`, `'0` -> `'1` |
| `bit-select` | Moves a literal bit index or slice bound up or down by one, within the declared range. | `data[3]` -> `data[2]`, `data[7:4]` -> `data[6:3]` |
| `deletion` | Removes one assignment, net initializer, or `case` item. | `q <= d;` removed, `case` branch removed |

`/`, `%`, and `**` are never swapped, so Mutants do not divide by zero.

Only design code is mutated. Booley skips:

- testbench, vendored, and generated files (by default);
- classes, programs, checkers, covergroups, and every assertion, because a
  mutated assertion fires and would count as a detection;
- parameter defaults, widths, delays, attributes, and the parts of a `for`
  loop that decide how long it runs;
- code a macro or include supplies, and the inactive branch of an `ifdef`
  (Booley applies the build's defines);
- spots that could make a loop run forever or drive a variable from two
  places, and `case` labels (moving one can overlap another item).

## How it chooses which Mutants

1. Booley collects the candidates from all scope files. Identical spots count
   once.
2. Within each operator, candidates are ranked by a hash of `--seed` and the
   Mutant's ID. The ID comes from the operator, file, and position inside its
   module, so editing code elsewhere in the file does not change it.
3. The six operators are then taken in turn, one candidate each, in the order
   of the table above, until `--count` Mutants are chosen. A small `--count`
   therefore covers different kinds of bugs instead of many of one kind.

If fewer candidates exist than `--count`, all of them run and the report
says so. The knobs are `--scope` (where to look), `--count` (how many), and
`--seed` (which sample); there is no filter by operator. The same RTL, scope,
count, and seed always choose the same Mutants.

## Reading results

The report starts with what reproduces the run, then the tally and the
verdict:

```text
[mutation_test] 10 Mutants (seed 0, count 10, generator 2, pyslang 9.0.0)
[mutation_test] sim sweep: 10/10 tested              4m
  8 detected (1 timeout), 0 invalid, 2 not detected
  detected: 8/10 (required: 8/10, default 80% of --count)
  NOT DETECTED [arithmetic-logical] rtl/alu.sv:42: 'a + b' -> 'a - b'
…
RESULT: PASS (8/10 detected)
```

Survivors are listed with file, line, and the original and mutated code: each
is a spot your tests do not check.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Detected Mutants reach the threshold. | `0` |
| `FAIL` | Fewer Mutants were detected than required. | `1` |
| `ERROR` | No verdict: a failing baseline, a Target that cannot run, an unusable `--scope` entry, an invalid option, or a build or infrastructure failure. | `2` |

Each run's files are in its numbered run directory, under `flow-reports/` in
the Booley Directory or `--report-dir`
([Reports](FLOW_REFERENCE.md#reading-results)):

| File | What it holds |
|---|---|
| `mutation/<N>/campaign/mutation-results.md` | The Mutants and the outcome of each. |
| `mutation/<N>/campaign/mutation-specs.md` | Every Mutant: operator, file and line, original and mutated code. |
| `mutation/<N>/campaign/manifest.json` | The run's identity (seed, count, generator and pyslang versions, scope hashes) and each Mutant's status. |
| `mutation/<N>/campaign/variants/` | The mutated source of each Mutant. |
| `mutation/<N>/campaign/baseline.log`, `mutant-logs/` | The simulator output of the baseline and of each Mutant. |

## Configuration (`[flows.mutation]`)

```toml
[flows.mutation]
enabled = true         # false turns the Flow off
timeout_ms = 1800000   # default: 30 minutes for the whole run
```

`timeout_ms` (or `--timeout`) sizes `--count auto`: the budget is divided to cap
the number of Mutants, so a larger limit allows more of them. Over MCP it also
raises the wait before the call hands back a run id. The command line does not
stop a run when the budget passes. See
[Flow and Specialist availability](CONFIG.md#flow-and-specialist-availability-enabled).

## Goals

A `MUTATION:{target}` Goal is met by this Flow's result: the Flow records the
`mutation_score` criterion. Your own runs never count as Goal evidence. A Goal
sets the campaign with `total: N`, `min_detected: K`, or `auto: true` in place of
`--count` and `--min-detected`; without them it requires 8 of 10 Mutants.
