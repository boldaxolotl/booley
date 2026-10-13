# Time tokens

Time arguments to `bwave` query subcommands (`-t`,
`--at`, `<T1> <T2>` for `diff`) accept a *typed* token:
an integer with an optional unit suffix.

## Grammar

```
time-token := integer suffix?
suffix     := 'c' | 't' | 'ns' | 'us' | 'ms' | 'ps'
time-range := time-token (':' time-token?)? | ':' time-token
```

A `time-range` (used by `-t`) accepts open-ended forms:
`5c:`, `:100ns`, or a single token (matches one point).

## Resolution rules

| Input         | Sync mode                  | Async mode                       |
|---------------|----------------------------|----------------------------------|
| `100` (bare)  | cycle 100                  | **ERROR**: bare int is ambiguous  |
| `100c`        | cycle 100                  | counted edge of cycle 100        |
| `100t`        | counted cycle at tick 100  | tick 100                         |
| `100ns`       | counted cycle at 100ns     | tick 100ns/timescale             |
| `100us` etc.  | same, scaled              | same, scaled                     |

In **sync mode** every token resolves to a cycle number.
In **async mode** every token resolves to a raw
simulation tick. Sync tick and physical tokens select the cycle whose
counted rising edge is last at or before the instant; instants before the
first counted edge resolve to cycle 0. The clock phase and reset policy
(`--reset`, `--with-reset`) determine the counted grid. Async `Nc` uses
that same grid; `0c` selects the effective start (reset deassertion, or
zero with `--with-reset`). Cross-unit cycle resolution requires a clock;
when the trace has no detected clock those forms error.

## Why bare integers are rejected in async mode

Async mode requires explicit units to prevent an integer copied from
a sync-mode invocation from silently changing its meaning. Use:
`100c` if you meant cycles, `100t` if you meant ticks,
`100ns` if you meant physical time.

## When timescale is missing

Physical-time tokens (`*ns`/`*us`/`*ms`/`*ps`) require
the `.fst` store to carry a VCD timescale. Every modern
simulator emits one; if the parser couldn't recover it,
you'll get a "no VCD timescale available" error.

## --before / --after

`--before N` and `--after N` are bounds, not tokens:
they accept bare integers in both modes (cycle in sync,
tick in async). These bounds are plain `i64` values.

## Examples

```bash
# Sync mode — bare int means cycle
bwave value foo.fst --at 100

# Sync mode — same, explicit
bwave value foo.fst --at 100c

# Sync mode — select the counted cycle at the physical instant
bwave value foo.fst --at 500ns

# Async mode — explicit unit required
bwave value foo.fst --at 500ns --async
bwave value foo.fst --at 50000t --async

# Async mode — bare int rejected
bwave value foo.fst --at 500 --async   # ERROR

# Mixed-unit range
bwave wave foo.fst -t 5c:200ns -s "tb.dut.*"

# diff takes T1 / T2 as tokens too
bwave diff foo.fst 5c 100ns -s "tb.dut.*"
```
