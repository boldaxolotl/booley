# bwave diff

## Semantics

Compare signal values between two time points. Only
signals whose value *changed* between `T1` and `T2` are
printed; identical signals are omitted. Good for "what
got disturbed between checkpoints A and B?".

Order doesn't matter: B-Wave swaps `T1` and `T2` so the
earlier point is the first column. Without `-s`, every
signal is compared.

## Output shape

Text mode: one row per changed signal, two value
columns:

```
# diff time 0 vs 5000
b                                        @0=0            @5000=2
```

`--format json` exits with code 2: `JSON output is not implemented for diff; use find/value/stats/list/signal/wave/distance/overview`.

## Examples

```bash
bwave diff sim.fst 100 200
bwave diff sim.fst 100 200 -s "tb.dut.*"
```
