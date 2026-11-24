# Markers

Markers are named time annotations. The native option is deliberately
`wave`-only because the horizontal wave table is the only native output with
columns where an annotation can be rendered.

## Native command matrix

| Command | `--marker` contract |
|---|---|
| `wave` | Accepted and rendered above the matching time column. |
| `build`, `list`, `signal`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, `stuck`, `schema`, `docs`, `skill` | Rejected during argument parsing with exit 2. |

This matrix concerns the native option. The Python wrapper's persisted marker
names remain usable as time references on other commands; see
[Wrapper integration](#wrapper-integration-bwave-markers).

## Native `wave` usage

```text
--marker NAME TIME
```

The option is repeatable. TIME uses the same grammar as `-t`
(`reference/time-tokens`) and must be non-negative.

Each in-window marker becomes a label above its column. In async mode, B-Wave
adds a column at the marker tick even if no selected signal transitions there.
Markers outside the `-t` window are silently omitted.

```text
$ bwave wave trace.fst -s clk -s data -t 4:9 --marker err_start 5 --marker done 8
              err_start        done
    cycle  4          5  6  7     8
      clk  1          1  1  1     1
data[7:0]  3          4  5  6     7
```

## Wrapper integration: `bwave markers`

The Booley Python wrapper persists named cycle references per registered trace
alias, and each checkout has its own:

1. Register a trace: `bwave register sim.fst --as dut`. A traced
   `booley flow sim` run registers `@<target>` and the default alias itself;
   markers already set on an alias stay on it when a later traced run
   replaces its trace.
2. Set markers: `bwave markers @dut set err_start 1234`.
3. Use names where a command accepts a cycle, for example
   `bwave @dut value --at err_start` or
   `bwave @dut diff err_start err_done`.

A marker name needs a non-digit character and cannot start with `-`, contain
whitespace or `:`, be a time token such as `5c` or `10ns`
(`reference/time-tokens`), or be a bwave subcommand such as `wave`. So a typed
time always means that time, and `:` stays free to separate the two ends of a
`-t` range. `bwave markers set` refuses a name that breaks these rules and
suggests one, such as `at_5c`.

The wrapper substitutes those names into time arguments before invoking the
native binary. Stored cycles are passed as explicit `Nc` tokens where the
native command accepts typed time. For `wave`, the wrapper additionally passes
each persisted marker as `--marker NAME Nc`, so the native renderer draws its
label.

The native binary does not persist markers and does not resolve names across
invocations. Native `--marker` annotates only the current `wave` output.

## Examples

Persist markers through the wrapper and reuse them across commands:

```bash
bwave markers @dut set err_start 1234
bwave markers @dut set err_done 1450
bwave @dut value --at err_start -s "*state*"
bwave @dut diff err_start err_done -s "*state*"
bwave @dut wave -t err_start:err_done -s "*err*"
```

Annotate one native wave invocation without persistence:

```bash
bwave wave sim.fst -s "*err*" -t 1200c:1500c \
    --marker err_start 1234c --marker err_done 1450c
```

Use physical time in async mode:

```bash
bwave wave sim.fst --async -s "*fsm*" -t 1us:2us \
    --marker request 1250ns --marker response 1275ns
```

## Collision and range rules

- Repeating a marker name updates it; the last occurrence wins.
- Distinct names at the same time share a comma-separated label.
- Negative marker times are rejected during argument parsing.
- Markers annotate output; they do not alter signal values or query matching.
