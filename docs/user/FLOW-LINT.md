# Lint (`lint`)

`lint` checks RTL for mistakes without simulating it. Each lint Target does one
of two kinds of checks: structural and semantic checks (such as width mismatches
and undeclared names) or style and naming checks. To get both, declare one
Target of each kind and run them together. Lint takes seconds,
so run it after each RTL edit and before simulation or synthesis. It does not
prove the design behaves correctly; that is what [Simulation](FLOW-SIM.md) is
for. For what all Flows share (choosing Targets, options, logs, exit codes),
see [Running Flows](FLOW_REFERENCE.md).

## Running lint

1. Check that the Project has a lint Target (`booley targets --for lint` lists
   them). Project Setup normally creates one; the
   [Targets](TARGETS.md#verilator-lint-target) recipes show how to add one.
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
| `--scope <path,...>` | Keep only findings whose file path contains one of the listed strings, so `rtl/fifo.sv` and `rtl/core/` both work. The whole Target is still linted, and errors are reported regardless of scope. |

## Reading results

Each Target prints one line with its linter, raw finding count, and number of
files linted. The console then shows the first five distinct findings, and the
run ends with one verdict:

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | No findings. | `0` |
| `WARN (N warnings)` | The linter ran and reported findings. | `1`, or `0` with `warnings_as_errors = false` |
| `FAIL` | The linter rejected the RTL, for example a syntax error or an undeclared signal. | `1` |
| `ERROR` | No verdict: missing linter, timeout, or a Target whose `toplevel` module is not in any of its files (lint would check nothing). | `2` |

The full list of findings is in `lint_report.json`, and each Target's complete
linter output is in its log; the verdict card prints both paths. A `LINT:{target}`
Goal is met only when that Target has no findings, whatever
`warnings_as_errors` says.

## Waivers and rules

Waive a known-acceptable finding in the Target, not in the RTL, so the waiver is
reviewed and versioned with the rest of the lint setup. Waived findings are not
reported. Recipes: [Verilator lint Target](TARGETS.md#verilator-lint-target)
(waiver file) and [Verible lint Target](TARGETS.md#verible-lint-target) (style
rules and waivers). [Target authoring](TARGETS.md#target-authoring) lists which
EDA tools a lint Target can use.

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
