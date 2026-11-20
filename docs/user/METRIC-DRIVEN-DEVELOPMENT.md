# Metric-Driven Development

Metric-Driven Development tracks the numbers that matter most for your RTL
design, its Target Metrics, at every commit as the design evolves. Use it to
accept a finished piece of work. Do not use it while you iterate; there,
plain `booley flow` runs are the loop, and each one is faster and shows more.

A metric is a number a Flow reports for one Target:

- **Lint:** warnings and errors.
- **Simulation:** failing tests, total tests, and the cycle count of each test.
- **Coverage:** line, branch, expression, toggle and cover-property coverage.
- **ASIC synthesis:** area in μm² and kGE, cell count, and per clock the
    Fmax, critical path, worst negative slack and worst hold slack.
- **FPGA implementation:** LUTs, flip-flops, BRAM tiles, DSPs, and the same
    per-clock timing as ASIC synthesis.

Adoption is optional and happens in stages. A Project that declares no
Target Metrics is never measured and nothing here applies to it.

**Contents**

1. [Why track metrics](#why-track-metrics)
2. [How it works](#how-it-works)
3. [Adopt it in stages](#adopt-it-in-stages)
4. [Write `metrics.toml`](#write-metricstoml)
5. [Check the file](#check-the-file)
6. [Measure a commit](#measure-a-commit)
7. [Give the Developer Agent a relative target](#give-the-developer-agent-a-relative-target)
8. [Read the Measurement Report](#read-the-measurement-report)
9. [Base runs](#base-runs)
10. [Limits](#limits)

## Why track metrics

- **No silent regressions.** The Developer Agent cuts area, and Fmax drops
    by half. Without a bound on Fmax nobody notices until much later; with
    one, the measurement fails and the agent has to fix it.
- **The design's history in numbers.** Every Measured Commit records its
    metrics, so you see how area, timing and coverage moved over time and can
    weigh one trade-off against another.
- **Hard numbers suit agents.** A Developer Agent iterates well against a
    bound it can check, such as `area_kge_max = 60`, and poorly against
    "make it smaller".

## How it works

1. **You choose the metrics.** For each Target, write the ones worth
    tracking in `.booley/metrics.toml`, once. You don't repeat them in each
    request.
2. **You ask for a Measured Commit.** End the request with "give me a
    Measured Commit".
3. **The Developer Agent finishes the work and runs `booley measure`.**
    Booley's own code runs the measurement, not the agent: every Flow named
    in `metrics.toml`, in a clean checkout of the commit, the same way each
    time. Numbers from two commits are therefore safe to compare.
4. **An unmet bound sends the agent back to work.** It fixes the design,
    commits and measures again, up to 3 times. It may not loosen
    `metrics.toml` without your approval.
5. **You read the Measurement Report.** A green header means every bound is
    met; see [Read the Measurement Report](#read-the-measurement-report).

## Adopt it in stages

Start with no metrics and add them as the design settles. A Project with no
`[<target>.<flow>]` table in `metrics.toml` measures nothing.

1. **Early design: no metrics.** Targets and tests change daily, so any bound
    would only get in the way. Use plain `booley flow` runs.
2. **Tests stabilize: add `sim` tables.** One `[<target>.sim]` table per
    Target, with the defaults' `tests_failed_max = 0`, makes "every test
    passes" part of the acceptance.
3. **Targets stabilize: track area, timing and coverage.** Add
    `track = [...]` entries (see [Write `metrics.toml`](#write-metricstoml))
    so each measurement reports the metric without judging it. A few
    measurements show its real range and noise.
4. **Numbers settle: turn tracked metrics into bounds.** Choose each bound
    with slack outside the range you saw: a `_max` bound above the highest
    value, a `_min` bound below the lowest. There is no tolerance setting;
    the slack is the number you write. The slack stops at the metric's valid
    range (see [Write `metrics.toml`](#write-metricstoml)).

The `booley-setup` skill asks "Enable metrics now or later?" during
[Project Setup](SETUP.md). `booley init` itself writes only the two default
lines (`[defaults.sim]` and `[defaults.coverage]`, each with
`tests_failed_max = 0`) and no table, so nothing is measured until you add one.

## Write `metrics.toml`

`metrics.toml` sits in the Booley Directory, `.booley/` at the top of your
repository. It holds one table per Target and Flow, `[<target>.<flow>]`, for
these Flows: `lint`, `sim`, `coverage`, `synth`, `fpga` and `mutation`.

Inside a table:

- `<metric>_max` is a bound at most that number, and `<metric>_min` a bound at
  least that number. Bounds are absolute; there is no relative bound.
- A bound lies within its metric's valid range, edges included. The coverage
  metrics and `mutation_score` are percentages from 0 to 100. Every other
  metric is at least 0, except `wns_ns` and `whs_ns`: slack can be negative.
- A metric reported per clock or per Test takes a prefix, `<clock>.<metric>`
  or `<test>.<metric>`. Booley splits a key on its last dot, and the whole key
  must be quoted: `"clk_i.fmax_mhz_min" = 400`. Unquoted, TOML reads the dot
  as a nested table, which `metrics.toml` rejects as a load error.
- A `mutation` table also sets how many Mutants and which sample; see
  [Measuring the mutation score](FLOW-MUTATION.md#measuring-the-mutation-score).
- `track = ["cell_count"]` reports a metric in every measurement without a
  bound, so you see its value over time.
- `off = ["tests_failed_max"]` removes a bound that `[defaults.<flow>]` would
  add to this table.
- `notes = { area_kge_max = "after DSP sharing" }` records why a bound is what
  it is. A note names a bound in force in this table, its own or one from
  `[defaults.<flow>]`, spelled as that bound's key; when a later commit
  changes that bound, the report shows the note beside the change.

`[defaults.<flow>]` adds its bounds to every table of that Flow. It never
creates a table, so a Target with no table of its own is still not measured.
A table needs at least one metric the Flow reports, as a bound or in `track`.

```toml
[defaults.sim]
tests_failed_max = 0

[defaults.coverage]
tests_failed_max = 0

[synth_core.synth]
area_kge_max = 60
"clk_i.fmax_mhz_min" = 400
"clk_i.wns_ns_min" = 0
track = ["cell_count"]
notes = { area_kge_max = "after DSP sharing" }

[sim_core.lint]
warnings_max = 0
errors_max = 0

[sim_core.sim]
"smoke.cycle_count_max" = 1000

[sim_core.coverage]
line_coverage_min = 90

[sim_fifo.sim]
off = ["tests_failed_max"]
track = ["tests_failed"]
```

Here `synth_core` must meet its area and timing bounds, `sim_core` must lint
clean, finish the `smoke` test within 1000 cycles and reach 90 percent line
coverage, and `sim_fifo` only reports how many tests fail.

<details markdown>
<summary>In stealth mode</summary>

Booley reads the rules from the committed `HEAD` of the Booley Directory's own
repository, so commit `metrics.toml` there with `git -C .booley commit`.
See [Stealth Mode](STEALTH.md).

</details>

## Check the file

`booley measure --validate` checks the `metrics.toml` of your working tree. It
needs no commit, measures nothing, takes no other option, and lists every load
error at once. Run it in a container terminal.

Bound values are absolute numbers: `tests_failed_max = 0`, never `"+10%"`. A
percentage value is an error that says so.

Where Booley can give a corrected line, it prints it as a `write instead:`
line. Three kinds of entry get one: an unquoted prefixed key, a relative-bound
key name, `<metric>_increase_at_most`, `<metric>_increase_at_least`,
`<metric>_reduce_at_most` or `<metric>_reduce_at_least`, and a bound outside
its metric's valid range, which gets the nearest edge of the range. An unknown
metric lists the metrics of its Flow instead.

```text
[sim_core.sim] smoke: a table holds bounds at its top level, and an unquoted dotted key is a nested table; quote the key
  write instead: "smoke.cycle_count_max" = 1000
```

```text
[sim_core.sim] tests_failed_increase_at_most: only absolute bounds exist (`_max` is at most, `_min` is at least)
  write instead: tests_failed_max = <absolute number>
```

```text
[sim_core.coverage] line_coverage_min: line_coverage must be within [0, 100], got 100.01
  write instead: line_coverage_min = 100
```


`--validate` also warns when a per-clock key or a tracked metric names a clock
that no stored measurement of its table reported, and lists the clocks that
were reported. The warning does not change the exit status.

`booley cheat --metrics` prints each Target's metrics as `metrics.toml` text,
with the keys already quoted. Save the blocks you keep in
`.booley/metrics.toml`, give each bound a number, and delete the rest.

Then commit the file: a measurement reads the `metrics.toml` committed with
the commit it measures.

## Measure a commit

1. **Commit your work.** `booley measure` never measures uncommitted work.
2. **Run `booley measure [<sha>]`** in a container terminal, or ask the
    Developer Agent for a Measured Commit. It measures `HEAD` by default, in
    a clean checkout, one child Flow per table, stores the Measurement Run
    and prints the report. Measuring `HEAD` notes any uncommitted change to a
    tracked file and any submodule whose working tree differs from the
    commit's pin, because neither is measured.
3. **Read the exit status** in the table below.
4. **On an unmet bound, fix forward.** Make a new commit, or amend the
    commit, then measure again. Booley stores a Measurement Run for the
    commit's tree, so amending only the message keeps a commit Measured.

The Developer Agent reports the verdict line, the failing rows and the paths
of the reports, and cancelling it stops the measurement's Flows. The
Dashboard shows each session's measurement; see
[The dashboard](USAGE.md#the-dashboard).

A commit that is already Measured is not measured again: the command prints
its stored run, starts no Flow and exits with status 0. `--target` checks and
measures its Target even then. A commit whose latest run is unmet, errored,
stopped or cancelled is measured again without `--again`.

| Option | Effect |
|---|---|
| `--base <sha>` | Compare with this commit, or with its merge-base with the measured commit when it is not an ancestor. See [Base runs](#base-runs). |
| `--all` | Evaluate every table. Without it, the first unmet or errored table stops the run and the later tables are "not evaluated". |
| `--again` | Measure a commit that is already Measured again and store a second run. |
| `--target <name>` | Measure only this Target's tables. The run is never stored and never makes the commit Measured; its reports stay in the `measure` Job's directory under `.interactive_logs/standalone/<run_id>/`. |
| `--json` | Print the report as JSON. |

| Exit status | Meaning |
|---|---|
| `0` | Every enforced bound is met. |
| `1` | A bound is unmet. |
| `2` | A Flow errored (also when a bound is unmet too), `metrics.toml` declares no table, `--target` names a Target with no table ("nothing to measure"), or the measurement could not run. |
| `130` | Ctrl-C cancelled the measurement. A run with a finished Flow is stored as cancelled, never as measured. |

## Give the Developer Agent a relative target

Bounds in `metrics.toml` are absolute. A target that depends on where the work
started, such as "area may grow by at most 10%", belongs in your request. The
Developer Agent then measures the base first, and you check the Δ column of
the report.

*Reduce the area of `synth_core` by at least 5% without losing timing. Measure
the base first, then give me a Measured Commit.*

## Read the Measurement Report

The report starts with a verdict line and a header whose color says whether
the work is accepted:

- **Red:** a bound is unmet or a Flow errored.
- **Green:** the commit is Measured, and nothing is amber.
- **Amber:** everything else. The header names the reason. A rule change
    shows as "rules loosened" or "tests changed"; a `--target` run shows "not
    stored", and a cancelled run shows its status.

Failing rows come first, and the metric table follows. A simulation or
coverage table with failing tests lists them, and an unmet lint or mutation
bound lists the lint findings or survived Mutants behind it, each with the path
of the Flow's report holding the full results.

Tracked metrics that errored or were not evaluated are counted after the
enforced total, not in it, as in
`3 of 3 enforced met; 3 tracked not evaluated`.

The report also compares your rules with the Base's. A loosened bound is
shown, never refused, with its note or "(no note)" beside it, so a reviewer
sees it. Only a bound still in force carries a note, so a bound turned `off`,
moved to `track` or removed, and a removed table, always show "(no note)" and
still mark the report "rules loosened". Give the reason for such a change in
the commit message.

<details markdown>
<summary>In stealth mode</summary>

Your rules are compared with the Booley Directory's own repository, including
rules committed in its local `main` but not yet pushed. `--base` moves only the
Base of your RTL. See [Stealth Mode](STEALTH.md).

</details>

## Base runs

The Base is the merge-base of your commit with the default branch; `--base`
names another one. A base run is an ordinary stored run of the Base's tree.
Booley never starts one by itself and it never decides a verdict. When one
exists, the report adds the Δ column and warns where a metric got worse.

Without one, the report prints the command to create it:
`booley measure --all <base>`. A commit on the default branch's own history
has no Base; name an earlier commit with `--base`.

<details markdown>
<summary>In stealth mode</summary>

The base run is the latest run of the Base's tree measured under the same
rules as your rules comparison. When no run was, the latest run of that tree is
used and the report says "its rules differ". See [Stealth Mode](STEALTH.md).

</details>

## Limits

- Git LFS files and files with filter attributes cannot be measured.
- The measurement checkout has no `.git`, so a generator, Pre-Sim Command or
  post-setup hook that calls `git` fails there.
- The checkout holds only the commit's tracked files. An input your Project
  builds and Git ignores reaches it through the post-setup hook (see
  [Worktree setup hooks](CONFIG.md#worktree-setup-hooks-booleyhooks)), which
  `booley measure` runs in the checkout. Its output goes to `post-setup.log` in
  the `measure` Job's directory; if the hook fails, the measurement stops with
  exit status 2 and names the hook and that log.
- Local results are advisory. The measurement store is writable from the
  Sandbox, and a deliberate read of the live tree by absolute path is
  possible. A run in CI is the trusted one.
- Heavy generated artifacts, such as a Linux image or a large firmware build,
  are out of scope.

If Booley itself misbehaves, see
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
