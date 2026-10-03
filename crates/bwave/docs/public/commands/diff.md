# bwave diff

## Synopsis

```bash
bwave diff <FST_FILE> <T1> <T2> [-s PATTERN[%RADIX] ...]
           [--async] [--clock PAT] [--reset PAT] [--with-reset]
           [--format text] [--limit N]
```

## Semantics

Compare signal values between two time points. Only
signals whose value *changed* between `T1` and `T2` are
printed; identical signals are omitted. Good for "what
got disturbed between checkpoints A and B?".

`T1` and `T2` are time tokens (see
`reference/time-tokens`). Order doesn't matter: B-Wave
swaps them internally so the smaller becomes the
"before" column. In sync mode bare integers are cycles;
async mode requires a unit suffix (`100t`, `100ns`).

`diff` strictly requires a built `.fst` store. It cannot
run against raw VCD.

## Defaults and requiredness

- `<FST_FILE>`, `<T1>`, `<T2>` all required.
- `-s PATTERN` optional. Without it, every signal in the
  store is compared, but only changed ones show in the
  output.

## Output shape

Text mode: one row per changed signal, two value
columns:

```
# diff time 0 vs 5000
b                                        @0=0            @5000=2
```

`--format json` exits with code 2: `JSON output is not implemented for diff; use find/value/stats/list`.

## Common errors

- **`requires a built waveform store`**: `diff` does not
  have a VCD fallback. Build one first:
  `bwave build <vcd> -o trace.fst`.
- **Empty output**: nothing changed between `T1` and
  `T2`. Verify the cycle window with `bwave signal` or
  pick wider endpoints.
- **Both columns show `'hX`**: the signal was X-valued
  at both points (often pre-reset). Add `--with-reset` if
  you wanted to see the pre-reset value explicitly, or
  pick `T1` after the reset deasserts.
- **`T1 > T2` is fine**: B-Wave normalises the order,
  and the output still labels them in ascending order.

## Examples

What changed between cycle 100 and 200?

```bash
bwave diff sim.fst 100 200
```

Narrow to the DUT only:

```bash
bwave diff sim.fst 100 200 -s "tb.dut.*"
```

Diff across an error window using markers (manually
resolved to cycles):

```bash
bwave diff sim.fst 1234 1450 -s "*err*" -s "*state*"
```
