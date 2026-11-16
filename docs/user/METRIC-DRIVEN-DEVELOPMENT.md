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
    the slack is the number you write.

The `booley-setup` skill asks "Enable metrics now or later?" during
[Project Setup](SETUP.md). `booley init` itself writes only the two default
lines (`[defaults.sim]` and `[defaults.coverage]`, each with
`tests_failed_max = 0`) and no table, so nothing is measured until you add one.

## Write `metrics.toml`

`metrics.toml` sits in the Booley Directory, `.booley/` at the top of your
repository. It holds one table per Target and Flow, `[<target>.<flow>]`, for
these Flows: `lint`, `sim`, `coverage`, `synth` and `fpga`.

Inside a table:

- `<metric>_max` is a bound at most that number, and `<metric>_min` a bound at
  least that number. Bounds are absolute; there is no relative bound.
- A metric reported per clock or per Test takes a prefix, `<clock>.<metric>`
  or `<test>.<metric>`. Booley splits a key on its last dot, and the whole key
  must be quoted: `"clk_i.fmax_mhz_min" = 400`. Unquoted, TOML reads the dot
  as a nested table, which `metrics.toml` rejects as a load error.
- `track = ["cell_count"]` reports a metric in every measurement without a
  bound, so you see its value over time.
- `off = ["tests_failed_max"]` removes a bound that `[defaults.<flow>]` would
  add to this table.
- `notes = { area_kge_max = "after DSP sharing" }` records why a bound is what
  it is; when a later commit changes that bound, the report shows the note
  beside the change.

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
needs no commit, measures nothing, and lists every load error at once. Where
Booley can give a corrected line, it prints it as a `write instead:` line.
An unquoted prefixed key or a relative bound gets one; an unknown metric lists
the metrics of its Flow instead.

```text
[sim_core.sim] smoke: a table holds bounds at its top level, and an unquoted dotted key is a nested table; quote the key
  write instead: "smoke.cycle_count_max" = 1000
```

`booley cheat --metrics` prints each Target's metrics as `metrics.toml` text
you can paste, with the keys already quoted.

## Measure a commit

1. **Commit your work.** `booley measure` never measures uncommitted work.
2. **Run `booley measure`**, or ask the Developer Agent for a Measured Commit.
    Booley measures the commit in a clean checkout, one child Flow per table.
3. **Read the exit status.** 0 means every bound is met, 1 means a bound is
    unmet, and 2 means a Flow errored or the measurement could not run.
4. **On an unmet bound, fix forward.** Make a new commit, or amend the
    commit, then measure again. Booley stores a Measurement Run for the
    commit's tree, so amending only the message keeps a commit Measured.

[CLI-REFERENCE.md](CLI-REFERENCE.md) lists every flag and exit status.

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

Failing rows come first, and the metric table follows.

The report also compares your rules with the Base's. A loosened bound is
shown, never refused, with its note or "(no note)" beside it, so a reviewer
sees it.

## Base runs

The Base is the merge-base of your commit with the default branch; `--base`
names another one and is normalized the same way. A base run is an ordinary
stored run of the Base's tree. Booley never starts one by itself and it never
decides a verdict. When one exists, the report adds the Δ column and warns
where a metric got worse.

Without one, the report prints the command to create it:
`booley measure --all <base>`. A commit on the default branch's own history
has no Base; name an earlier commit with `--base`.

## Limits

- Git LFS files and files with filter attributes cannot be measured.
- The measurement checkout has no `.git`, so a generator or Pre-Sim Command
  that calls `git` fails there.
- Local results are advisory. The measurement store is writable from the
  Sandbox, and a deliberate read of the live tree by absolute path is
  possible. A run in CI is the trusted one.
- Heavy generated artifacts, such as a Linux image or a large firmware build,
  are out of scope.
- `mutation_score` is not a metric, because the mutation Flow has no
  `metrics.toml` table.

If Booley itself misbehaves, see
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
