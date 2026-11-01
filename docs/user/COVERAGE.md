# Simulation coverage

Coverage is part of the [`sim` Flow](FLOW-SIM.md): add `--coverage` to a
Simulation run to collect it. `sim` can collect Verilator coverage (line, branch, expression, toggle,
and cover properties) into a **Coverage Campaign**, check it against Coverage
Criteria, and hand it to the Coverage Analyst for waiver candidates and
testbench improvements.

## How coverage is measured

Coverage is measured per Target, per run:

1. Each selected test runs in its own simulator process and writes its own
   Verilator coverage database.
2. Booley merges those databases into one result for the Target: a point is
   covered if any test hit it. Per-test hit counts are kept, so you can see
   which test covered what.
3. Each metric's percentage is covered RTL points divided by eligible RTL
   points. Testbench and generated code are reported but not scored. When
   approved waivers apply (see [Collecting vs. gating](#collecting-vs-gating)),
   waived points are left out of the count.

What counts as RTL:

- Every file in the Target's resolved fileset **without** the `tb` tag. Tag
  every testbench file `tb`; an untagged one is scored as RTL.
- Only code elaborated under the testbench top gets coverage points. An RTL
  module that is never instantiated can produce no points.
- After collection, Booley warns about RTL files that produced no coverage
  points (usually a module that is never instantiated). The warning does not
  change the percentages. The check is per file and uses a simple text scan, so
  it can miss a file or report one by mistake.
- Points are per instance: a module instantiated four times contributes four
  sets of points, and each instance must be exercised.

Each Target gets its own Coverage Campaign. Nothing is merged across Targets or
across runs.

## Quick start

```bash
# 1. Collect coverage for the full suite
booley flow sim --target sim_soc --coverage
```

Then call the `coverage_analyst` Specialist from your Developer Agent session with
`campaign="<reports>/sim/12/targets/sim_soc/coverage.json"` for Waiver Candidates
and testbench improvements. The verdict card prints the exact
`coverage.json` path to pass as `campaign`.

## Requirements

- Every selected Target must use Verilator. Selecting any Icarus Target rejects
  the whole run before anything is built.
- Only `--mode simulate` (the default). `--trace` can be combined with
  `--coverage`.
- Tests run one at a time, one simulator process per test (including cocotb).
- Testbench properties such as excluding reset from coverage, or hooks for a
  custom C++ main, live in the Target's `.core`. See
  [Coverage configuration](#native-coverage-configuration).

## Collecting vs. gating

- Every coverage run applies the approved waivers: waived points are reported
  `waived` and counted in `waived_points`, and they are left out of the eligible
  points and percentages. An invalid approval file, or an approval that does not
  match the collected points, blocks that Target's evaluation and exits 2, with
  or without a Criterion. Fix the approvals or use `--no-waivers`. When
  collection is incomplete (already exit 2), approvals are not matched against
  the partial points and nothing is waived.
- **Ungated** (no Coverage Criterion): Booley collects and stores the Campaign
  with evaluation `not_requested`. Use it to explore.
- **Gated** (Goal Mode with a `COVERAGE:{target}` Goal): Booley also checks
  the thresholds. Only a persisted `pass` satisfies
  the Criterion. A Goal remains unmet until strict evidence passes; see
  [Coverage waivers at review](#coverage-waivers-at-review). The thresholds
  alone decide the Goal: a Test Run that fails its checks does not make it
  unmet, as long as the run still wrote its coverage data.
- **Raw numbers** (`--no-waivers`): applies no approved waivers. With a
  Coverage Criterion it needs `--diagnostic`; otherwise Booley exits 2 before
  anything builds. The thresholds are then evaluated on raw numbers, and no
  Criteria are recorded.

Which tests run: the Test Runs you name with `--test`/`--tests-file`, otherwise the
Target's registered suite minus `tests.toml` skips, the same as any `sim` run.
A coverage run accepts every selector form, including several Test Runs of one
Test (`--test stress+short --test stress+soak`, or `--test stress@x5`). Each
Test Run is one simulator process with its own native database, and the
merged coverage is the union over all of them. A
Criterion's `tests` list never picks tests; it says which tests the evidence
must come from. If what ran differs from that list, coverage is still collected
but gated evaluation is blocked (suite mismatch), so pass the listed tests
explicitly.

## Coverage Criteria and waivers

A coverage Goal declares thresholds per Target:

```json
{"family": "COVERAGE", "target": "sim_counter", "tests": "all",
 "metrics": {"line": 90, "branch": 80}}
```

A human can waive points that are unreachable or out of scope; see
[Approved coverage waivers](#approved-coverage-waivers).

## Coverage waivers at review

In Goal Mode, the Coverage Analyst's code screens Waiver Candidates and saves
those ready for human review in the local Goal Record's `waiver-candidates.json`.
An `unreachable` candidate needs a zero-hit point. A candidate carries no
approval authority; only strict coverage evidence meets a Goal.

Ask the Developer Agent to propose each needed candidate with
`goal_propose_change(operation="create", kind="waiver", goal_key=...,
candidate_id=..., rationale=...)`. Inspect the point, source, Campaign,
coverage delta, and justification before approving or rejecting the exact
proposal. The client form records your decision and reason; chat fallback
records your quoted instruction. No candidate is accepted by default.

Approval writes the bound waiver and any review proof into the configured
[approval directory](#approved-coverage-waivers) and records the
change in the Change Log. Rerun coverage to obtain fresh strict evidence;
approval itself does not meet a Goal. A stale or invalid candidate needs a new
Campaign and Analyst screening. A rejection is retained for that exact point
and source content. Finish includes the approved changes and evidence in its
Review Package, leaving merge to your separate instruction.

## Results

Simulation, collection, and threshold evaluation are reported independently: a
failing test can still yield valid, passing coverage, and a fully passing suite
can miss a threshold. The exit code reflects simulation and collection:

| Exit | When |
|---:|---|
| `2` | Coverage could not be trusted: preflight rejection, collection or infrastructure failure, incompatible data, or blocked evaluation (e.g. an invalid waiver or a suite mismatch). |
| `1` | A test failed. |
| `0` | No test failed and coverage can be trusted. A missed threshold also exits `0`: the Coverage Criterion is recorded as not met and the verdict card names the missed metric. |

With several Targets, each keeps its own simulation, collection, and evaluation
results in the report even when another Target decides the exit code.

## Where the evidence lives

Each Target gets its own reference and nested Coverage Campaign under the run's
numbered report directory (qualified Target selectors are percent-encoded):

```text
<reports>/sim/<N>/targets/<target>/
  coverage.json              booley.coverage-campaign-reference/v1 pointer
  simulation.json            the matching Target simulation results
  campaign/
    manifest.json            the enclosing Simulation Campaign
    work-items/<item>/attempts/<attempt>/coverage-campaign/
      coverage.json          current Coverage Campaign manifest
      coverage-points.jsonl.gz
      native/raw/            one Verilator database per Test Run
      native/merged/         merged Verilator database
      hooks/                 hook evidence, when collected
```

Pass the numbered Target-level `coverage.json` reference to the Coverage Analyst.
It authenticates the nested Coverage Campaign and reads the matching completed Simulation result;
it does not accept the nested manifest directly. Never edit or pass the point
store directly, and keep the reference and enclosing Simulation Campaign
together.

## Analyzing a Campaign

`coverage_analyst` explains one Campaign: what is uncovered, likely reasons,
which tests to add, and Waiver Candidates for human review.

Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact coverage.json>"`.
An optional `instruction` focuses it. Pick its model like any Specialist's,
under [`[models]`](CONFIG.md#model-selection-models).

It never runs simulation, changes Criteria, or approves waivers; in Goal
Mode Booley records its candidates for
[approval at review](#coverage-waivers-at-review). See
[CLI-REFERENCE.md](CLI-REFERENCE.md#coverage_analyst).

## Cleaning up completed Campaigns

Raw Verilator coverage databases are large. Booley never deletes evidence
automatically; prune an exact run explicitly:

```bash
# Drop one Target's raw/merged databases; the Campaign stays analyzable
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --native-target sim_soc

# Remove the whole run (no re-analysis afterwards)
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --full
```

- `--full` refuses when later resumed runs depend on this one; add
  `--include-dependents` to remove them together.
- If reports live outside the standard Booley Directory locations, `--full` may ask
  for `--project-data "$PROJECT_DATA"`.
- Pruning refuses runs that are still active. `--full` also refuses files the run
  did not produce; `--native-target` removes the Target's whole `native/` directory.
  An interrupted cleanup is safe to retry with the same arguments.

## Configuration

Coverage settings come from three places: the Target's `.core` definition,
each coverage Goal, and an optional `[coverage.waivers]` table in
`booley.toml`. Other Simulation settings are on the
[`[flows.sim]` page](FLOW-SIM.md#configuration-flowssim).

### Native coverage configuration

Collection is explicit (`booley flow sim --coverage`, permanent alias `--cov`,
or MCP `coverage: true`). Configuration and Criteria never turn it on.
Stable testbench properties belong to the Target's `.core` definition:

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

`reset_included` is boolean and defaults to `true`, counting initialization and
reset. Set it to `false` only when each test can prove exactly one successful
`booley_coverage_start()` after reset and before writing its database.
`custom_main_hooks` applies only to project-authored C++ mains; generated-main,
HDL-testbench, and Cocotb Targets omit it. A custom main must declare `write_hook`
and call `booley_coverage_write()` exactly once on every normal terminal path.
When reset is excluded, declare both hooks and call start before write. A
`start_hook` declaration is invalid when reset is included. Unknown or duplicate
hooks, missing declarations, and unproved calls are errors. These controls cannot
be overridden in `[flows.sim]` or invocation flags.

A coverage Goal sets policy independently for each Target:

```json
{"family": "COVERAGE", "target": "sim_counter", "tests": "all",
 "metrics": {"line": 90, "branch": 80, "toggle": 75}}
```

Each Target must be explicit. `tests` is `all` or a nonempty list of
[Test Run selectors](GOAL-MODE.md#goals-that-name-test-runs), for example
`["reset", "stress+soak@x5"]`. Metrics are `line`,
`branch`, `expression`, `toggle`, and `cover_property`; select at least one.
Each `min_pct` is a finite non-boolean number in `(0, 100]`. Thresholds combine
with AND and use exact rational arithmetic; displayed rounding does not decide
truth. Zero eligible points blocks evaluation. Only RTL-closure points enter
denominators; testbench, generated, foreign, FSM, and covergroup records remain
unscored. Simulation verdicts do not change coverage threshold truth, and a
failed Test Run does not make a coverage Goal unmet.

#### Approved coverage waivers

The Booley Directory's `booley.toml` optionally selects one project-wide approval directory:

```toml
[coverage.waivers]
anchor = "project_data_repository"
directory = "coverage-waivers"
```

`anchor` is exactly `rtl_repository` or `project_data_repository`; the latter is
the Booley Directory.
`directory` is a safe relative path. Approval files mirror RTL paths: source
`rtl/counter.sv` uses `coverage-waivers/rtl/counter.sv.toml`. A human-authored file
has this shape (replace the placeholders with exact retained evidence):

```toml
schema = "booley.coverage-waivers/v1"
source = "rtl/counter.sv"
source_sha256 = "sha256:<exact source SHA-256>"

[[approval]]
id = "reserved-counter-branch"
target = "acme:demo:counter:1#sim_counter"
point_id = "<complete cp1: point identity from the Campaign>"
reason = "excluded"
justification = "Reserved behavior is intentionally excluded."
approved_by = "verification-owner@example.test"
approved_at = "2026-09-09T09:00:00Z"
approval_ref = "review:CR-1042"
```

For `reason = "unreachable"`, add an `approval.proof` table with a `kind`, a
safe `reference` relative to the approval directory, and exact `sha256` of the
proof file. Use `kind = "formal"` for a formal-tool result and `kind = "review"`
for a written human review argument, such as a Markdown file; review-time
approval writes one as `proofs/<waiver-id>.md`. Lowercase `*.toml` paths below
the approval directory are reserved for approval documents, so proof artifacts use another extension. Every non-TOML
file below the directory must be named by a proof reference in a valid
approval document; otherwise loading reports it as unreferenced. `excluded`
cannot carry proof. Both reasons yield `waived`; only
exact RTL points are waivable. Loading rejects unsafe paths/symlinks, malformed
or duplicate approvals, stale sources, unknown Target identities, missing proofs,
and candidate content. One unmatched or invalid approval blocks that Target's
evaluation, and none of that Target's approvals apply. Analyst Waiver Candidates have no approval authority of their own and
cannot be copied here by hand; they become approval files through a human-approved
`goal_propose_change` waiver proposal (see [Coverage waivers at review](#coverage-waivers-at-review)).

## Troubleshooting

Resume refusals, Coverage Analyst input problems, collection and evaluation
failures, and Waiver Candidate issues are covered in
[Coverage Campaign diagnostics and retention](TROUBLESHOOTING.md#coverage-campaign-diagnostics-and-retention).
