# Lint (`lint`)

`lint` checks RTL for mistakes without simulating it and produces a list of
findings. What it checks depends on the linter each lint Target uses: one
linter catches width mismatches and undeclared names, another flags style and
naming problems. [Supported EDA tools](SUPPORTED-EDA-TOOLS.md#built-in-flows)
lists the linters and what each one checks. Run several lint Targets together
to combine their checks into one list. Lint usually takes seconds, so run it
after each RTL edit and before simulation or synthesis. It does not prove the
design behaves correctly; that is what [Simulation](FLOW-SIM.md) is for. For
what all Flows share (choosing Targets, options, logs, exit codes), see
[Running Flows](FLOW_REFERENCE.md).

## Running lint

1. Check that the Project has a lint Target (`booley targets --for lint` lists
   them). Project Setup normally creates a Verilator Target and a Verible
   style Target;
   [Lint Targets](#lint-targets) shows how to add one.
2. Ask the Developer Agent, naming the lint Target or Targets:

    > *"Run lint on `lint_soc` and `lint_style` and explain every finding."*
    >
    > *"Lint only `rtl/fifo.sv` and fix what it reports."*

3. The agent runs each Target in turn and reports the findings with file, line,
   and rule. A finding reported by several Targets appears once.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow lint --target lint_soc,lint_style --scope rtl/fifo.sv
```

| Option | Effect |
|---|---|
| `--scope <path,...>` | Keep only findings whose file path contains one of the listed strings, so `rtl/fifo.sv` and `rtl/core/` both work. The whole Target is still linted, and errors are reported regardless of scope. Entries are checked against the Target's HDL file paths and the paths of reported findings; an entry that matches none ends the run with `ERROR` (exit `2`), naming the entry and a few linted paths, so a typo cannot pass. |

## Reading results

A run you start yourself prints one line per Target, then the findings left
after merging duplicates across Targets and applying `--scope`, then the
verdict.

Example (Verilator):

```text
[lint] lint_soc     [verilator] 7 warnings (target total), 12 files, 0.4s
[lint] 7 unique in-scope warnings
[lint]   %Warning-UNUSEDSIGNAL: rtl/fifo.sv:10:5 Signal is not used: 's10'
  …
[lint]   ... and 2 more (see /work/.booley/flow-reports/lint/3/lint_report.json)

RESULT: WARN (7 warnings)
See /work/.booley/flow-reports/lint/3/lint_report.json for the full warning list (rule/file:line/message).
```

The Target line shows its linter, everything the linter reported (before
merging and `--scope`), and the number of files linted. The console lists the
first five findings. The `RESULT` line gives one verdict:

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | No findings. | `0` |
| `WARN (N warnings)` | The linter ran and reported findings. | `1`, or `0` with `warnings_as_errors = false` |
| `FAIL` | The linter rejected the RTL, for example a syntax error. | `1` |
| `ERROR` | No verdict: missing linter, timeout, a Target whose `toplevel` module is not in any of its files (lint would check nothing), or a `--scope` entry that matches no linted file. | `2` |

<details markdown>
<summary>With Verilator</summary>

`FAIL` also covers errors about what the RTL means, such as an undeclared
signal.

</details>

<details markdown>
<summary>With Verible</summary>

Only a parse error, such as a syntax error, gives `FAIL`. Every rule violation
is a warning.

</details>

A Target that could not be linted cleanly prints `ERROR:` on its own line in
both the `FAIL` and `ERROR` cases; the `RESULT` line tells them apart. On a
`FAIL`, both lines quote the linter's first error and point at the full log.

Example (Verilator):

```text
[lint] lint_soc     ERROR: %Error: rtl/fifo.sv:3:1: Can't find definition of variable: 'zzz' (full log: .booley/flow-reports/lint/4/artifacts/lint_soc/run.log)
…
RESULT: FAIL — lint_soc: %Error: rtl/fifo.sv:3:1: Can't find definition of variable: 'zzz' (full log: .booley/flow-reports/lint/4/artifacts/lint_soc/run.log)
```

Each run's files are in its numbered run directory (`lint/3/` above), under
`flow-reports/` in the Booley Directory or `--report-dir`
([Reports](FLOW_REFERENCE.md#reading-results)):

| File | What it holds |
|---|---|
| `lint/<N>/lint_report.json` | Every finding with its rule, file, line, message, and the Targets that reported it; the error of each Target that could not be linted; and, per Target, the linter, raw finding count, files linted and log path. `passed` is `false` whenever there are findings. |
| `lint/<N>/artifacts/<target>/run.log` | The linter's complete output for that Target: every error after the first, and the context around each finding. |

`flow-reports/lint_report.json` is a copy of the latest run's report.

A `LINT:{target}` Goal is met only when that Target has no findings within the
run's `--scope`, whatever `warnings_as_errors` says. A run with any `--scope`
entry that matches no linted file ends in `ERROR` and records no passing
result.

## Lint Targets

A lint Target sets `flow: lint` and `flow_options.tool` to `verilator` or
`verible` (`veriblelint` also works). A Target with any other tool doesn't
show up in `booley targets --for lint`. Name it `lint_<subject>`; see
[Target naming](TARGETS.md#target-authoring).

Waive a known-acceptable finding in the Target, not in the RTL, so the waiver is
reviewed and versioned with the rest of the lint setup. Waived findings are not
reported. Each linter takes its waivers, and Verible its style rules, from a
file the Target includes:

### Verilator lint Target

To waive Verilator warnings, add a `.vlt` control file with `file_type: vlt`
and include its fileset in the lint Target. The type must be exactly `vlt`:
an untyped `.vlt` file, or a guessed type such as `verilatorConfig`, is
silently ignored. Paths are relative to the `.core` file:

```yaml
CAPI=2:
name: acme:ip:top:1.0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
  lint_config:
    files:
      - lint/waivers.vlt: {file_type: vlt}
targets:
  lint_structural:
    flow: lint
    flow_options:
      tool: verilator
      verilator_options: [-Wall]   # optional: more warnings, unrelated to the waiver
    filesets: [rtl, lint_config]
    toplevel: top
```

`lint/waivers.vlt` starts with the control-file directive, then lists what to
waive:

```text
`verilator_config
lint_off -rule WIDTHTRUNC -file "*top.sv"
```

This waives `WIDTHTRUNC` in files ending in `top.sv` (the pattern also matches
FuseSoC's build-tree copy); every other warning stays on. Keep patterns
specific, and narrow further with `-lines` (see Verilator's
[control-file reference](https://verilator.org/guide/latest/control.html)).
Add `lint_config` to other Verilator Targets only where the same waiver
should apply.

### Verible lint Target

A style Target selects the RTL plus a rules file and, optionally, waivers:

```yaml
CAPI=2:
name: acme:ip:top:1.0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
  lint_config:
    files:
      - lint/rules.cfg: {file_type: veribleLintRules}
      - lint/waivers.txt: {file_type: veribleLintWaiver}
targets:
  lint_style:
    flow: lint
    flow_options: {tool: verible}
    filesets: [rtl, lint_config]
    toplevel: top
```

Use at most one rules file; waiver files are optional.

## Configuration (`[flows.lint]`)

Besides the shared `enabled` and `timeout_ms` (default `120000`, two minutes per
Target), lint has one setting:

```toml
[flows.lint]
timeout_ms = 120000
warnings_as_errors = false   # default true
```

`warnings_as_errors = false` makes a warnings-only run exit `0`, so a CI gate
fails only on `FAIL` and `ERROR`. The console and report still show every
warning, and `lint_report.json` still records `passed: false`.
