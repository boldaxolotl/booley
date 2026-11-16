# Metrics

**Target Metrics** are the numbers a commit has to meet: for each Target and
Flow, the bounds you enforce and the values you only track. You declare them
in `.booley/metrics.toml`, and `booley measure` checks one commit against
them. A commit whose full measurement met every enforced bound is a
**Measured Commit**. Declaring metrics is optional: a Project without
`metrics.toml` is unaffected. While you iterate, run a single
[Flow](FLOW_REFERENCE.md); measure when you want the commit's verdict.

## Declaring metrics

`metrics.toml` holds one `[<target>.<flow>]` table per Target and Flow:

- A `<metric>_max` or `<metric>_min` key is an enforced bound.
- `track` lists metrics that are reported but not enforced.
- `[defaults.<flow>]` adds bounds to every table of that Flow; `off` in a
  table removes one of them.

`booley cheat --metrics` prints the measuring rules, then each Target's
metrics as `metrics.toml` text. Save the blocks you keep as
`.booley/metrics.toml`, give each bound a number, and delete the rest. Then
check the file in a container terminal:

```bash
booley measure --validate
```

`--validate` checks the file in your working tree, measures nothing, and takes
no other option. Commit the file: a measurement reads the `metrics.toml`
committed with the commit it measures.

<details markdown>
<summary>In stealth mode</summary>

Commit `metrics.toml` in the Booley Directory's own repository: a measurement
reads it from that repository's last commit. See [Stealth Mode](STEALTH.md).

</details>

## Measuring a commit

In a container terminal:

```bash
booley measure [<sha>]
```

It measures one commit (`HEAD` by default, never uncommitted work) in a clean
checkout, one child Flow per `metrics.toml` table, stores the **Measurement
Run** and prints the report. Measuring `HEAD` notes on stderr any uncommitted
change to a tracked file and any submodule whose working tree differs from the
commit's pin, because neither is measured.

| Option | Effect |
|---|---|
| `--base <sha>` | Compare with this commit, or with its merge-base with the measured commit when it is not an ancestor. The default Base is the commit's merge-base with the default branch. |
| `--all` | Evaluate every table. Without it, the first unmet or errored table stops the run and the later tables are "not evaluated". |
| `--again` | Measure a commit that is already Measured again and store a second run. |
| `--target <name>` | Measure only this Target's tables. |
| `--json` | Print the report as JSON. |

A commit that is already Measured is not measured again: the command prints
its stored run, starts no Flow and exits with status 0. A commit whose latest
run is unmet, errored, stopped or cancelled is measured again without
`--again`.

A `--target` run is never stored and never makes the commit Measured. Its
report and Flow reports are kept in the `measure` Job's directory under
`.interactive_logs/standalone/<run_id>/`, beside its log. A Measured commit
still shows its stored run unless you add `--again`.

| Exit status | Meaning |
|---|---|
| `0` | Every enforced bound is met. |
| `1` | A bound is unmet. |
| `2` | A Flow errored (also when a bound is unmet too), `metrics.toml` declares no table, `--target` names a Target with no table ("nothing to measure"), or the measurement could not run. |
| `130` | Ctrl-C cancelled the measurement. A run with a finished Flow is stored as cancelled, never as measured. |

The Dashboard shows each session's measurement; see
[The dashboard](USAGE.md#the-dashboard).
