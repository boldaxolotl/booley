# Coverage

Coverage tells you how much of your RTL the tests actually exercise: which
lines, branches, expressions, toggles, and cover properties were hit, and
which never were. Use it to judge whether a testbench is thorough, to find
logic no test reaches, or, in Goal Mode, to require a minimum coverage before a
change counts as done. It is part of the [Simulation Flow](FLOW-SIM.md) and
works only on Verilator Targets; an Icarus Target cannot collect coverage.
Coverage does not replace pass/fail checks: a fully covered design can still
be wrong.

## What is measured

Verilator splits the design into **coverage points**, small things that a
test either hits or misses. Each metric counts a different kind of point:

| Metric | One point is | Covered when |
|---|---|---|
| `line` | a block of statements that always run together | the block runs |
| `branch` | one outcome of an `if` or `case`: the `if` taken, its `else` taken, one `case` item | that outcome happens |
| `expression` | one combination of a Boolean expression's terms that decides its result, such as `a` false in `a && b` | that combination occurs |
| `toggle` | one bit of a signal changing in one direction: `count[3]` 0→1 and `count[3]` 1→0 are two points | that bit makes that change |
| `cover_property` | one SystemVerilog `cover property` | the property matches |

One hit by one test is enough. Verilator also records FSM and covergroup
data; Booley keeps it in the Campaign but gives it no percentage, so no
threshold applies to it. The metric names in the table are the ones
[Coverage Goals](#coverage-goals) use.

## Collecting coverage

1. Make sure the Target simulates with Verilator and every testbench file in
   its fileset carries the `tb` tag. An untagged testbench file is counted as
   RTL and skews the numbers.
2. Ask the agent for a coverage run, for example "Run sim_soc with coverage."
   The agent runs `booley flow sim --target sim_soc --coverage` (short form
   `--cov`). It runs the same Test Runs as a plain `sim` run
   ([selecting Test Runs](FLOW-SIM.md#test-runs-test-variants-and-seeds)).
3. Read the result (next section).
4. To find out what is missing and why, ask the agent to run the
   [Coverage Analyst](#analyzing-a-campaign) on the run.

If any selected Target uses Icarus, the whole run is refused before anything
builds. Coverage runs only in the default `--mode simulate`. `--trace` can be
combined with `--coverage`.

Generated or custom C++ mains, HDL testbenches, and cocotb all work.

## Reading results

Each Target gets its own **Coverage Campaign**: the coverage record of one
Target in one run. The easiest way to read it is to ask the agent, for example
"What coverage did sim_soc reach?". The agent gets every metric's percentage,
any threshold and whether it was met, and the Campaign's location.

A run you start yourself prints one line per Target:

```text
sim_soc: simulation PASS · coverage collection COMPLETE · evaluation NOT_REQUESTED (Simulation Campaign …)
  manifest: /work/.booley/flow-reports/sim/12/targets/sim_soc/campaign/manifest.json
  report: /work/.booley/flow-reports/sim.json
```

The first line has three verdicts:

- **simulation**: whether the tests passed, as in
  [Simulation results](FLOW-SIM.md#reading-results).
- **coverage collection**: `COMPLETE`, or `COLLECTOR_ERROR`, `INCOMPLETE` or
  `INCOMPATIBLE` followed by a diagnostic code in brackets. Anything but
  `COMPLETE` means the numbers can't be trusted.
- **evaluation**: whether the thresholds were met. `NOT_REQUESTED` means
  there were none to check, which is the normal case outside a
  [Coverage Goal](#coverage-goals). `FAIL` names the first metric that
  missed, for example `evaluation FAIL (branch: observed 1/2 points;
  displayed 50%; minimum 80%)`. `BLOCKED` gives a code, such as an invalid
  [waiver](#coverage-waivers).

The `manifest` and `report` paths are the same as in any `sim` run; see
[Simulation results](FLOW-SIM.md#reading-results).

### Where the numbers are

The terminal doesn't print the percentages. Each run gets a numbered
directory under `flow-reports/` in the Booley Directory (or `--report-dir`),
`sim/12/` above. Coverage results are in two of its files.

**`sim/<N>/report.json`** has each Target's percentages, under
`detail.targets.<target>`:

```json
"sim_soc": {
  "simulation": "pass",
  "collection": "complete",
  "evaluation": "not_requested",
  "coverage_metrics": {
    "line":       {"percent": 98.58, "threshold": null, "met": null},
    "branch":     {"percent": 77.19, "threshold": null, "met": null},
    "expression": {"percent": 79.51, "threshold": null, "met": null},
    "toggle":     {"percent": 88.98, "threshold": null, "met": null}
  }
}
```

`threshold` and `met` are filled in when a Coverage Goal sets a threshold
for that metric. A metric the design has no points for, such as
`cover_property` in a design without `cover property` statements, is left out.

**`sim/<N>/targets/<target>/coverage.json`** is the Coverage Campaign. Pass
its path to the [Coverage Analyst](#analyzing-a-campaign) to find out what is
uncovered and why.

### How the numbers are counted

- Each test writes its own coverage data, and Booley merges them: a
  [point](#what-is-measured) is covered if any test hit it. Booley keeps which test hit what.
- A metric's percentage is covered RTL points divided by all RTL points.
  Testbench (`tb`-tagged) and generated code are reported but not counted.
  [Approved waivers](#coverage-waivers) remove their points from the count.
- Points are per instance: a module instantiated four times has four sets of
  points, and each instance must be exercised.
- Only code instantiated under the testbench top has points. Booley warns
  about RTL files that produced no points at all (usually a module that is
  never instantiated). The check is a simple per-file text scan, so it can
  miss a file or flag one by mistake. The warning does not change any number.
- Nothing is merged across Targets or across runs. To measure a combined
  suite, run all its tests in one coverage run.

Simulation results and coverage are judged separately: a failing test can
still produce valid coverage, and a passing suite can miss a threshold. The
exit code covers simulation and collection only:

| Exit | When |
|---:|---|
| `0` | No test failed and coverage was collected. A missed threshold also exits `0`; the result names the missed metric. |
| `1` | A test failed. |
| `2` | Coverage could not be trusted: the run was refused, collection failed, or evaluation was blocked (for example an invalid waiver or the wrong set of tests). |

With several Targets, each Target keeps its own results even when another
Target decides the exit code.

### Finding uncovered points

Ask the agent, which runs the [Coverage Analyst](#analyzing-a-campaign). For a
quick look yourself, every [coverage point](#what-is-measured) of the run is
in the point store, `coverage-points.jsonl.gz`, one JSON line per point. It
sits in the Campaign directory,
`sim/<N>/targets/<target>/campaign/work-items/*/attempts/*/coverage-campaign/`.
Inside the Sandbox, from that directory:

```bash
# Uncovered points per source file, worst first
zcat coverage-points.jsonl.gz | jq -r '
  select(.disposition.kind == "eligible" and ([.hits_by_run[]] | add // 0) == 0)
  | .identity.location.source' | sort | uniq -c | sort -rn | head

# Uncovered points in one file: line, metric, which point, instance
zcat coverage-points.jsonl.gz | jq -r '
  select(.identity.location.source == "rtl/fifo.sv" and .disposition.kind == "eligible"
         and ([.hits_by_run[]] | add // 0) == 0)
  | [.identity.location.start.line, .identity.metric,
     (.identity.subject | to_entries[0].value), .identity.hierarchy] | @tsv'
```

The second command prints one line per uncovered point, such as
`42  toggle  wr_ptr[2]:1->0  TOP.tb.dut.u_fifo`: bit 2 of `wr_ptr` on line 42
never fell from 1 to 0 in instance `u_fifo`. For a `branch` point the third
column is the outcome (`if`, `else`, a `case` item); for an `expression`
point, the term combination. Only `eligible` points count toward the
percentage, so the commands skip testbench, generated and waived ones. The
file's format can change between Booley releases.

## Coverage Goals

Outside Goal Mode, a coverage run just collects and stores the Campaign; there
are no thresholds to pass. In Goal Mode (host doc, not shipped), a coverage Goal sets
minimum percentages for one Target, for example "line coverage of sim_counter
at least 90% and branch at least 80% over the whole suite":

```json
{"family": "COVERAGE", "target": "sim_counter", "tests": "all",
 "metrics": {"line": 90, "branch": 80}}
```

- Metrics are the five in [What is measured](#what-is-measured).
  Name at least one; each threshold is a number above 0 and at most 100.
  Every threshold must be met. Booley compares exact values, so a displayed
  89.996% rounded to 90.00% does not meet 90.
- `tests` is `"all"` or a list of
  Test Run selectors (host doc, not shipped), such as
  `["reset", "stress+soak@x5"]`. It does not choose which tests run; it says
  which tests the measurement must come from. If the run's tests differ,
  coverage is still collected but the Goal is not evaluated, so run exactly
  the listed tests.
- The thresholds alone decide the Goal. A failed test does not make it unmet,
  as long as the run still wrote its coverage data. Add a simulation Goal for
  the same tests when they must also pass.
- If a metric in the Goal has no RTL points at all, evaluation is blocked.

To see coverage without waivers, add `--no-waivers`. When the Target has a
coverage Goal, this also needs `--diagnostic`, and the run then records no Goal
evidence.

## Coverage waivers

Some coverage points can never be hit (dead defaults, tied-off inputs) or are
deliberately out of scope. A waiver removes one such point from the count.
Each waiver is an **approval file** in your [waiver directory](#waiver-directory-coveragewaivers),
naming the person who approved it, so the agent can't raise coverage by
waiving what it failed to test.

Every coverage run applies the approved waivers, in Interactive Mode and in
Goal Mode: waived points are reported as `waived` and left out of the
percentages. If an approval file is invalid, or does not match the current RTL
(for example the source changed), that Target's evaluation is blocked and the
run exits `2`. Fix or remove the approval, or rerun with `--no-waivers`. If one
approval for a Target is bad, none of that Target's approvals apply.

How a waiver gets approved depends on the mode.

### In Goal Mode

1. The Coverage Analyst proposes **Waiver Candidates**: points it believes are
   unreachable or out of scope. A candidate counts for nothing on its own.
2. The agent asks you to approve the candidates it needs. You see the point,
   its source, the coverage change it would make, and the justification, and
   you approve or reject each one with a reason. Nothing is approved by
   default.
3. Approving writes the approval file (and, for an unreachable point, a
   written review argument as proof) into your waiver directory and records
   the change in the Goal's Change Log. A rejection is remembered for that
   point until its source changes.
4. Ask the agent to rerun coverage. Approval does not meet a Goal; only a new
   run with the waiver applied does.

### In Interactive Mode

There is no approval step: the Coverage Analyst still suggests points that
look unreachable or out of scope, but nothing records them, and a waiver
exists only once an approval file does.

1. Ask the agent which points could be waived, for example "Which uncovered
   branches in fifo.sv are unreachable?". The Analyst names each point by its
   `point_id` and gives its reason and evidence.
2. Decide which ones to waive, and ask the agent to write approval files for
   them, or write them yourself. The [format](#waiver-directory-coveragewaivers)
   is under Configuration.
3. Review every file before you keep it, especially `approved_by`,
   `justification` and, for an `unreachable` point, the proof. Nothing else
   checks that a person approved it.
4. Rerun coverage to see the waivers applied.

## Analyzing a Campaign

The **Coverage Analyst** is a Specialist that explains one Coverage Campaign:
what is uncovered, the likely reasons, which tests to add, and in Goal Mode
which points look like Waiver Candidates. Ask the agent, for example "Why is
branch coverage on sim_soc so low?". Its model only reads the evidence: it
never runs simulation, changes Goals, or approves waivers. Pick its model like
any Specialist's, under [`[models]`](CONFIG.md#model-selection-models).

To run it yourself inside the Sandbox, pass the Target's
[`coverage.json`](#reading-results):

```bash
booley specialist coverage_analyst --campaign <path to coverage.json>
```

## Disk space

Coverage runs are large: each one keeps a raw Verilator database per Test Run
plus a merged one, often tens of megabytes per test. Booley never deletes
them; see [Cleaning up reports](FLOW_REFERENCE.md#cleaning-up-reports).

## Configuration

Coverage is collected only when a run asks for it; no setting turns it on.
Other Simulation settings are on the
[Simulation page](FLOW-SIM.md#configuration-flowssim).

### Testbench settings in the `.core`

Two optional settings live with the Target in its `.core` file:

```yaml
targets:
  sim_counter:
    flow: sim
    flow_options:
      tool: verilator
      booley:
        coverage:
          reset_included: false
          custom_main_hooks: [start_hook, write_hook]
```

- `reset_included` (default `true`) counts initialization and reset. Set it to
  `false` to leave reset out; each test must then call
  `booley_coverage_start()` exactly once after reset.
- `custom_main_hooks` is only for Targets with your own C++ `main`. Declare
  `write_hook` and call `booley_coverage_write()` exactly once on every normal
  exit path. With `reset_included: false`, also declare `start_hook` and call
  start before write; `start_hook` is invalid when reset is included.

Booley reports unknown, duplicate, or missing hooks as errors.

### Waiver directory (`[coverage.waivers]`)

Waivers cannot be approved until `booley.toml` names where approval files go:

```toml
[coverage.waivers]
anchor = "project_data_repository"
directory = "coverage-waivers"
```

`anchor` is `project_data_repository` (the Booley Directory) or
`rtl_repository` (the RTL repository). `directory` is a relative path below it.

Approval files mirror RTL paths: waivers for `rtl/counter.sv` go in
`coverage-waivers/rtl/counter.sv.toml`. A hand-written file looks like this:

```toml
schema = "booley.coverage-waivers/v1"
source = "rtl/counter.sv"
source_sha256 = "sha256:<SHA-256 of rtl/counter.sv>"

[[approval]]
id = "reserved-counter-branch"
target = "acme:demo:counter:1#sim_counter"
point_id = "<point id from the Campaign>"
reason = "excluded"
justification = "Reserved behavior is intentionally excluded."
approved_by = "verification-owner@example.test"
approved_at = "2026-09-09T09:00:00Z"
approval_ref = "review:CR-1042"
```

- `reason` is `excluded` (out of scope) or `unreachable`. An `unreachable`
  waiver also needs a `proof` table with `kind` (`formal` for a formal-tool
  result, `review` for a written argument), `reference` (the proof file's path
  relative to the waiver directory), and `sha256` of that file. `excluded`
  takes no proof.
- `.toml` files in the directory are approval files; proofs need another
  extension, such as `.md`. Every other file in the directory must be named
  by some approval's proof.
- A file that still contains the Analyst's candidate fields is rejected
  (`COV_WAIVER_CANDIDATE_FORBIDDEN`). Write a normal approval for the point
  instead, using its `point_id`.
