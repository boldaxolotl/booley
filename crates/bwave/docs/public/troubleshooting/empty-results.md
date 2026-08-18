# Empty results

A query returned zero rows. The two loudest causes now
*are* errors: a query where **every** `-s` pattern
matches nothing exits 2 with
`ERROR: no signals match pattern(s) ...`, and a store
with **no signals at all** (a header-only trace) exits 2
with `ERROR: waveform store has no signals`. If you got
zero rows *with* exit 0, the patterns matched — walk the
checklist below in order; the first four causes account
for most such cases.

## 0. Store has no signals at all (exit 2)

`ERROR: waveform store has no signals (header-only
trace? ...)` means the .fst parsed but declares zero
signals. The classic producer is a Verilator sim traced
via the auto-generated `--main`, which opens the dump
and writes only the header. Re-run the sim with a custom
C++ `--exe` main that drives tracing, then rebuild the
store. `list` still answers on such a store (exit 0)
but prints the same ERROR line instead of a signal
count — in `--format json` mode too, where it also
rides in the envelope's `warnings[]`.

`bwave build` itself now refuses a zero-signal VCD
(exit 2, `input VCD declares no signals`), so a store
that trips this error was written by an external
producer — typically Verilator dumping FST directly —
not by `build`.

## 1. Pattern matched no signals

B-Wave's signal-pattern semantics surprise people. A
bare name is **suffix-match against the full
hierarchical path**:

- `dmem_addr` matches `tb.dut.dmem_addr` (suffix
  match) but NOT `tb.dut.dmem_addr_next` (the suffix
  differs).
- `*dmem_addr*` matches both.
- `*dmem*addr` also matches both.
- `dmem_wr[0]` matches the element of that name. A
  trailing all-digit index is a literal, not a glob
  character class; `mem[0-3]` still globs.

When *all* patterns miss, the query hard-fails (exit 2,
`ERROR: no signals match pattern(s) '...' (N signals in
store; ...)`). When only *some* miss, the query still
runs (exit 0) and each dropped filter is named on stderr
(`# WARNING: no signals match '...' — filter dropped`),
so a partially-empty result explains itself. JSON-mode
`find`/`value`/`stats` still emit a parseable empty
envelope on stdout in the total-miss case — the exit
code carries the failure.

Fix: always sanity-check with `list`.

```bash
bwave list sim.fst -s "*your_pattern*"
```

If `list` returns nothing, the pattern is wrong (or the
signal isn't in the store at all, see step 4).

## 2. Reset phase ate the events

By default sync mode skips output until the reset
deasserts. If the events you're looking for happened
during reset, they're invisible.

Check the header line for the reset detection result:

```
# reset: tb.dut.rst_n  deasserts @ cycle 5
```

If your event was before cycle 5 (in this example),
add `--with-reset`:

```bash
bwave find sim.fst "*error*" 'h1 --first --with-reset
```

See `troubleshooting/reset-skipping` for the full
detection rules.

## 3. Time range outside the simulation

`-t 50000:60000` on a 10000-cycle sim returns nothing
silently. Verify the simulation length with `stats`:

```bash
bwave stats sim.fst -s "*clk*"
```

The header reports `total_cycles` and `total_ticks`.
Pick a `-t` window inside that range.

## 4. Signal value never matched

You searched for `'d5` but the signal only ever held
0-3. Verify with the value histogram in `stats`:

```bash
bwave stats sim.fst -s "*state*"
```

The `value_hist` field tells you exactly which values
the signal took and how often. If `'d5` isn't in the
histogram, no match is possible.

Also check radix: are you searching for `'d255` but the
store holds `'hFF`? Both should match (they're the same
value), but `255` vs `'hFF` with width prefixes can
diverge if the stored width doesn't match the literal
width. See `troubleshooting/radix-pitfalls`.

## 5. Wrong clock detected (sync mode only)

If sync mode picked a non-toggling signal as "clock",
every cycle gets the same tick and find/sample silently
do nothing meaningful.

Header:

```
# clock: tb.dut.maybe_clk  period=0ns
```

`period=0ns` is the smoking gun. Override:

```bash
bwave find sim.fst "*error*" 'h1 --clock "*real_clk*"
```

See `troubleshooting/clock-detection`.

## 6. Edge keyword vs. value confusion

`find sig 'h1` matches every cycle where `sig` *holds*
1. `find sig rising` matches only the cycles where
`sig` *transitioned* low->high. These are different
queries; pick the right one.

If `sig` rises once at cycle 5 and holds high until the
end of sim, `find sig 'h1` returns thousands of matches
and `find sig rising` returns one.

## 7. Virtual signal failed silently

If you used `--virtual` and the predicate failed to
parse or resolve, B-Wave prints an error to stderr but
runs the query as if the virtual didn't exist. Check
stderr for `ERROR: ...` lines, and check the exit
code: virtual-def failures exit non-zero.

## 8. `--first` / `--last` semantics

`--first` returns the first match in the `-t` window.
If you scoped the window with `--after 1000` but the
first match is at cycle 500, you get nothing because
500 is outside the implied range.

## Debug checklist

When in doubt, drop modifiers and broaden:

```bash
# 1. does the signal exist?
bwave list sim.fst -s "*name*"

# 2. did it ever change?
bwave stats sim.fst -s "*name*"

# 3. show me the trace
bwave signal sim.fst -s "*name*" --with-reset --limit 200

# 4. now narrow back down
bwave find sim.fst "*name*" 'h1 --first
```
