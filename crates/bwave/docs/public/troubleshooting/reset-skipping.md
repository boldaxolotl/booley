# Reset skipping

By default B-Wave skips output from cycle 0 until the
reset deasserts. This keeps queries focused on "real"
simulation rather than initialisation noise. If your
query is missing events from the reset window, this is
the reason.

## Detection rules

B-Wave looks for a 1-bit signal whose name matches
`*rst*` (case-insensitive). Like clock detection, the
shallowest scope wins. Common matches:

- `tb.rst_n`
- `tb.dut.reset`
- `tb.dut.sync_rst`

The header line reports the choice:

```
# reset: tb.dut.rst_n  deasserts @ cycle 5
```

Active-low vs active-high is inferred from the initial
value:

- If the signal starts at 0 and rises, it's treated as
  active-low (`*_n` convention). Deassert = rising
  edge.
- If the signal starts at 1 and falls, it's treated as
  active-high. Deassert = falling edge.
- If the signal never changes, no deassert is detected
  and reset-skipping is disabled (output starts at
  cycle 0).

The deassert cycle is the first cycle *after* the edge.

## Disabling the skip

Two ways:

1. `--with-reset`: include the reset phase in
   output. Cycle numbering still starts at 0 (not
   negative), but you'll see the reset transitions.

   ```bash
   bwave signal sim.fst -s "*state*" --with-reset
   ```

2. `--reset NONE`: not supported as a literal flag;
   pass an unmatching pattern instead:

   ```bash
   bwave signal sim.fst -s "*state*" --reset "no_such_signal"
   ```

   When the pattern matches nothing, reset detection
   fails silently and skipping is disabled. This is a
   hack; prefer `--with-reset`.

## Overriding the reset signal

If auto-detection picks the wrong reset (e.g. a
testbench reset when you wanted the DUT reset):

```bash
bwave signal sim.fst -s "*state*" --reset "*tb.dut.local_rst*"
```

The pattern follows the same suffix-match rules as
`-s`. Wrap in wildcards if you want substring
matching.

## Symptoms of reset-skip biting

- `find ... --first` returns the first post-reset
  match, not the actual first. Add `--with-reset` to
  see pre-reset matches.
- `signal` / `wave` start "late": cycle 5, 7, 100
  instead of 0.
- `value --at 0` errors or returns reset-phase
  values; `value --at -1` rejected (cycles can't be
  negative without `--with-reset`).
- Distance / sample queries miss pairs that span the
  reset boundary.

## Cycle numbering and `--with-reset`

With `--with-reset`, cycle 0 is the first rising clock
edge after sim start. Cycles before the reset deassert
are still numbered 0, 1, 2, ...; they're not
negative. The cycle count is independent of the reset.

The reset deassert cycle is informational only. With
the flag set, it's a label in the header rather than a
skip threshold.

## Multiple resets

B-Wave picks one reset (the shallowest `*rst*`
match). If your design has multiple resets (e.g.
async global reset + sync local reset), only one is
used for skipping. To skip on a different one:

```bash
bwave signal sim.fst -s "*" --reset "*local_rst*"
```

To skip on neither, use `--with-reset` and ignore reset
in your mental model.

## No reset at all

Designs without any reset signal (some
combo-only test benches, simple sim setups) get no
auto-skip. The query starts at cycle 0. Header:

```
# reset: <none detected>
```

This is fine, no `--with-reset` needed.

## Quick checks

When in doubt, dump the reset cycle explicitly:

```bash
# what cycle does the reset deassert?
bwave find sim.fst "*rst_n" rising --first --with-reset

# show the boundary
bwave signal sim.fst -s "*rst*" -s "*state*" \
    -t 0:20 --with-reset
```
