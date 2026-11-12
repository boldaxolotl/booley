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

## Reading results

The report starts with what reproduces the run, then the tally and the
verdict:

```text
[mutation_test] 10 Mutants (seed 0, count 10, generator 2, pyslang 9.0.0)
[mutation_test] sim sweep: 10/10 tested              4m
  8 detected (1 timeout), 0 invalid, 2 not detected
  detected: 8/10 (required: 8/10, default 80% of --count)
  NOT DETECTED [operator_change] rtl/alu.sv:42: 'a + b' -> 'a - b'
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

`timeout_ms` (or `--timeout`) bounds the whole run, not one test. It is also
what `--count auto` divides to cap the number of Mutants, so a larger limit
allows more of them. See
[Flow and Specialist availability](CONFIG.md#flow-and-specialist-availability-enabled).

## Goals

A `MUTATION:{target}` Goal is met by this Flow's result: the Flow records the
`mutation_score` criterion. Your own runs never count as Goal evidence. A Goal
sets the campaign with `total: N`, `min_detected: K`, or `auto: true` in place of
`--count` and `--min-detected`; without them it requires 8 of 10 Mutants.
