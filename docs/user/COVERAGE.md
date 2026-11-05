# Coverage

Coverage tells you how much of your RTL the tests actually exercise: which
lines, branches, expressions, toggles, and cover properties were hit, and
which never were. Use it to judge whether a testbench is thorough, to find
logic no test reaches, or, in Goal Mode, to require a minimum coverage before a
change counts as done. It is part of the [Simulation Flow](FLOW-SIM.md) and
works only on Verilator Targets; an Icarus Target cannot collect coverage.
Coverage does not replace pass/fail checks: a fully covered design can still
be wrong.

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

Generated or custom C++ mains, HDL testbenches, and cocotb all work. FSM and
covergroup records are kept but not scored, so no threshold applies to them.

## Reading results

Each Target gets its own **Coverage Campaign**: the coverage record of one
Target in one run. The result shows a percentage for each metric, the
thresholds you set (if any) and whether each was met, and the path of the
Campaign's `coverage.json`, which you pass to the Coverage Analyst. Where the
files live on disk is described in
[Flow reports](../internals/FLOW_REPORTS.md).

How the numbers are counted:

- Each test writes its own coverage data, and Booley merges them: a point is
  covered if any test hit it. Booley keeps which test hit what.
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

## Coverage Goals

Outside Goal Mode, a coverage run just collects and stores the Campaign; there
are no thresholds to pass. In [Goal Mode](GOAL-MODE.md), a coverage Goal sets
minimum percentages for one Target, for example "line coverage of sim_counter
at least 90% and branch at least 80% over the whole suite":

```json
{"family": "COVERAGE", "target": "sim_counter", "tests": "all",
 "metrics": {"line": 90, "branch": 80}}
```

- Metrics are `line`, `branch`, `expression`, `toggle`, and `cover_property`.
  Name at least one; each threshold is a number above 0 and at most 100.
  Every threshold must be met. Booley compares exact values, so a displayed
  89.996% rounded to 90.00% does not meet 90.
- `tests` is `"all"` or a list of
  [Test Run selectors](GOAL-MODE.md#simulation-cycle-count-and-coverage-goals), such as
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
Waivers are always approved by a human, so the agent can't raise coverage by
waiving what it failed to test.

In Goal Mode:

1. The Coverage Analyst proposes **Waiver Candidates**: points it believes are
   unreachable or out of scope. A candidate counts for nothing on its own.
2. The agent asks you to approve the candidates it needs. You see the point,
   its source, the coverage change it would make, and the justification, and
   you approve or reject each one with a reason. Nothing is approved by
   default.
3. Approving writes the waiver (and, for an unreachable point, a written review
   argument as proof) into your [waiver directory](#configuration) and
   records the change in the Goal's Change Log. A rejection is remembered for
   that point until its source changes.
4. Ask the agent to rerun coverage. Approval does not meet a Goal; only a new
   run with the waiver applied does.

Every coverage run applies the approved waivers: waived points are reported
as `waived` and left out of the percentages. If an approval file is invalid,
or does not match the current RTL (for example the source changed), that Target's
evaluation is blocked and the run exits `2`. Fix or remove the approval, or
rerun with `--no-waivers`. If one approval for a Target is bad, none of that
Target's approvals apply.

You can also write approval files by hand; the format is under
[Configuration](#configuration).

## Analyzing a Campaign

The **Coverage Analyst** is a Specialist that explains one Coverage Campaign:
what is uncovered, the likely reasons, which tests to add, and in Goal Mode
which points look like Waiver Candidates. Ask the agent, for example "Why is
branch coverage on sim_soc so low?". Its model only reads the evidence: it
never runs simulation, changes Goals, or approves waivers. Pick its model like
any Specialist's, under [`[models]`](CONFIG.md#model-selection-models).

To run it yourself inside the Sandbox, pass the `coverage.json` path from the
result:

```bash
booley specialist coverage_analyst --campaign <path to coverage.json>
```

## Cleaning up completed Campaigns

Coverage runs keep the raw Verilator databases, which are large. Booley never
deletes them automatically. To free space, prune one run. Take both values
from the `coverage.json` path in the result: for
`<reports>/sim/12/targets/sim_soc/coverage.json`, the reports root is
`<reports>` and the run number is `12`.

```bash
# Drop one Target's raw databases; the Campaign stays readable by the Analyst
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --native-target sim_soc

# Remove the whole run; nothing can analyze it afterwards
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --full
```

- `--full` refuses when later resumed runs depend on this one; add
  `--include-dependents` to remove them too. It may ask for
  `--project-data <Booley Directory>` when it has to recover an interrupted
  run.
- Runs that are still active are refused. An interrupted cleanup is safe to
  retry with the same arguments.

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
- Waiver Candidates from the Analyst can't be copied in by hand; they become
  approvals only through review in Goal Mode.
