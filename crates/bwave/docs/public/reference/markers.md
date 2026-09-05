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

The option is repeatable. TIME uses the same typed grammar as `-t`:

- In sync mode, a bare integer or `Nc` is a cycle.
- `Nt` is a raw simulation tick.
- `Nps`, `Nns`, `Nus`, and `Nms` are physical times.
- In async mode, the suffix is required; a bare integer is ambiguous and is
  rejected.
- Marker times must be non-negative.

Each in-window marker becomes a label above its column. In async mode, B-Wave
adds a column at the marker tick even if no selected signal transitions there.
Markers outside the `-t` window are silently omitted.

```text
              err_start              dma_done
              v                      v
              500         510         520         530
              |---------- |---------- |---------- |----------
clk           ^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_
state         'h2  'h2  'h2  'h5  'h5  'h5  'h0  'h0  'h0
```

## Wrapper integration: `bwave markers`

The Booley Python wrapper persists named cycle references per registered trace
alias:

1. Register a trace: `bwave register sim.fst --as dut`.
2. Set markers: `bwave markers @dut set err_start 1234`.
3. Use names where a command accepts a cycle, for example
   `bwave @dut value --at err_start` or
   `bwave @dut diff err_start err_done`.

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
