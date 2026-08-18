# Clock detection

Sync mode needs a clock. B-Wave picks one automatically
when a query loads the store (clock metadata is
re-derived from the FST content each time; nothing is
stored in sidecars), and reports its choice in the
header. If the choice is wrong, sync-mode results will
be off by clock-period factors or empty.

## Auto-detection algorithm

1. Walk the signal tree breadth-first (shallowest
   scope first).
2. For each 1-bit signal whose name matches `*clk*`
   (case-insensitive), measure the period (time
   between two adjacent rising edges).
3. The first match with a sensible period wins.

"Sensible period" means non-zero and roughly stable.
A signal that toggles randomly at irregular intervals
may be picked anyway: the algorithm only checks
period > 0, not regularity.

## What gets reported

The header:

```
# clock: tb.dut.clk  period=10ns
```

`period=10ns` is the smoking gun for a healthy pick.
`period=0ns` means the picked signal didn't toggle at
all in the recorded window: sync mode will produce zero
or garbage output.

## Overriding

`--clock PATTERN` forces a specific clock. Pattern
syntax is the same as `-s` (suffix-match by default,
wildcards otherwise):

```bash
bwave signal sim.fst -s "*state*" --clock "tb.dut.main_clk"
bwave signal sim.fst -s "*state*" --clock "*core_clk*"
```

The pattern must match exactly one 1-bit signal. If it
matches zero or multiple signals, B-Wave errors out:

```
ERROR: clock pattern '*clk*' matches 3 signals; narrow it
```

## Common detection failures

### Multiple clocks, wrong one picked

In multi-clock designs (CDC sim, multi-domain SoC),
the shallowest `*clk*` wins. This may be the slow
peripheral clock when you wanted the core clock.

Override explicitly:

```bash
bwave find sim.fst "*error*" 'h1 --clock "*core_clk*"
```

### Clock signal not named `*clk*`

Common offenders: `mclk`, `aclk`, `phi1`, `ck`,
`hclock`. The default heuristic misses these. Either:

- Override per-invocation: `--clock "*hclock*"`.
- Build with `--scope` to drop ambiguous clocks if
  the right one matches `*clk*` in the chosen
  subtree.

### Clock is multi-bit (e.g. clock + enable bundled)

The algorithm requires 1-bit signals. A bundled
`{clk, en}` 2-bit wire is silently skipped. Override
with a 1-bit clock signal.

### No clock at all

Sim has no clock (pure combo, testbench infra). B-Wave
reports:

```
# clock: <not detected>
```

Sync-mode commands degrade:

- `value --at N` treats `N` as a tick (`at_unit:
  "tick"` in JSON).
- `find` reports `unit: "tick"`.
- `stats` shows `total_cycles: null`,
  `clock_period_ns: null`.

This is graceful degradation, not an error. If you
really want sync semantics, you need to define a
clock-like signal upstream and rerun the sim.

Alternatively, use `--async` explicitly: same data,
clearer headers.

### Period drift

A clock with jitter (varying period) reports the
*first* measured period as the canonical period. Cycle
boundaries assume constant period, so jittery clocks
will misalign sampling vs. real RTL behaviour.

For accurate timing on jittery clocks, use `--async`
and work in ticks.

## Header symptoms cheat-sheet

| Header | Meaning |
|---|---|
| `# clock: tb.dut.clk  period=10ns` | healthy, proceed |
| `# clock: tb.dut.clk  period=0ns` | clock never toggled; pick another |
| `# clock: <not detected>` | no `*clk*` match; pick with `--clock` or use `--async` |
| `ERROR: clock pattern matches N signals` | tighten the pattern |
| `ERROR: clock pattern matches 0 signals` | check `bwave list -s "*clk*"` |

## Debug procedure

```bash
# what clocks are in the store?
bwave list sim.fst -s "*clk*"
bwave list sim.fst -s "*ck*"

# is the candidate actually toggling?
bwave stats sim.fst -s "*core_clk*"

# force the right one
bwave signal sim.fst -s "*state*" --clock "*core_clk*"
```

If `stats` reports `transitions=0` for a candidate
clock, it's not a clock in this sim: pick a different
one.

## Async fallback

When in doubt, drop sync mode:

```bash
bwave signal sim.fst -s "*state*" --async -t 0t:1000t
```

Async output is busier (every transition gets a row)
but immune to clock-detection problems. Useful as a
sanity check when sync output looks wrong.
