# bwave stuck

## Synopsis

```bash
bwave stuck <FST_FILE> [VALUE] [-s PATTERN[%RADIX] ...]
            [--async] [--clock PAT] [--reset PAT] [--with-reset]
            [--format text] [--limit N]
```

## Semantics

Report signals that never transitioned during the
simulation (after reset, unless `--with-reset` is set).
Stuck signals are usually one of:

- Tied-off inputs (legitimate constants).
- Disconnected wires (synthesis hazard).
- Dead control paths (FSM states never reached).
- A bug: the signal should have toggled but didn't.

If you supply `[VALUE]`, only signals stuck at that
specific value are reported. Useful for "find every wire
stuck at 0" or "find anything stuck at X".

`stuck` does not accept `--virtual` or `--marker`.

## Defaults and requiredness

- `<FST_FILE>` required.
- `[VALUE]` optional, positional. When omitted,
  *any-value-stuck* mode is used.
- `-s PATTERN` optional. Without it, every signal in the
  store is considered.
- Standard global options apply.

## Output shape

Text mode, one signal per row, with the constant value:

```
# Stuck signals: 1 of 1 analyzed
  constant  4-bit  stuck at A  (100%, 100ns)
```

With an explicit `VALUE` filter:

```
# Stuck signals: 1 of 1 analyzed (filter: A)
  constant  4-bit  stuck at A  (100%, 100ns)
```

`--format json` exits with code 2: `JSON output is not implemented for stuck; use find/value/stats/list`.

## Common errors

- **A signal you expected to be stuck is missing**:
  it transitioned at least once. Verify with
  `bwave stats -s "name"` (transitions = 0 means
  stuck).
- **Reset-phase signals all appear stuck**: `stuck`
  scans only the post-reset window by default. If a
  signal toggled only during reset, it shows here. Add
  `--with-reset` to include the reset window in the
  scan; that will show they *did* transition, and
  they'll drop off the list.
- **`[VALUE]` rejected as invalid**: values follow the
  same Verilog literal rules as `find` / `sample`. Bare
  hex (`FF`) is rejected; use `'hFF`. See
  `reference/verilog-literals`.

## Examples

Every stuck signal in the design:

```bash
bwave stuck sim.fst
```

Only signals stuck high:

```bash
bwave stuck sim.fst 'h1
```

Stuck-at-X audit (likely uninitialised flops):

```bash
bwave stuck sim.fst 'hX
```

Stuck signals inside a specific scope:

```bash
bwave stuck sim.fst -s "tb.dut.fifo.*"
```
