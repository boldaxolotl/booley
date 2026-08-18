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

| Input          | Sync mode                      | Async mode                          |
|----------------|--------------------------------|-------------------------------------|
| `100`  (bare)  | cycle 100                      | **ERROR**: bare int is ambiguous    |
| `100c`         | cycle 100                      | cycle 100 (resolved via clock)      |
| `100t`         | cycle 100/period               | tick 100                            |
| `100ns`        | cycle 100ns/period             | tick 100ns/timescale                |
| `100us` etc.   | same, scaled                   | same, scaled                        |

In **sync mode** every token resolves to a cycle number.
In **async mode** every token resolves to a raw
simulation tick. Cross-unit resolution requires the
clock period (`*ns -> cycle`, `*t -> cycle`); when the
trace has no detected clock those forms error.

## Why bare integers are rejected in async mode

Bare integers used to mean "cycles in sync, ticks in
async". That made it easy to copy a value from a
sync-mode invocation, paste it under `--async`, and
silently change the meaning. v0.2 makes you spell it:
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
tick in async, per the legacy convention). Suffix
support may be added later if needed; today they're
plain `i64`.

## Examples

```bash
# Sync mode — bare int means cycle
bwave value foo.fst --at 100

# Sync mode — same, explicit
bwave value foo.fst --at 100c

# Sync mode — convert physical time via clock period
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
