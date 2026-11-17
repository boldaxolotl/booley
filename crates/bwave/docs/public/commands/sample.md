# bwave sample

## Semantics

Each time `TRIGGER_PAT` equals `TRIGGER_VAL` (or matches
the named edge), capture a snapshot of the `-s` signals.
This answers the question "every time X fires, what was
Y?" It's useful for sampled-on-handshake debugging.

Think of it as `find` + a snapshot per match. The trigger
defines *when*; `-s` defines *what to capture*. Trigger
values, `--first`, `--last`, `--before`, `--after`, and
`--count` behave as in `find`. Row selection with Virtual
Signals: `reference/virtual-signals`.

## Output shape

Text mode prints time/name/value rows per trigger event:

```
1000 c 1
```

With `--count`:

```
1
```

`--format json` exits with code 2: `JSON output is not implemented for sample; use find/value/stats/list/signal/wave/distance/overview`.

## Common errors

- **Captured data is already updated**: sync mode reads
  the trigger and the captured values post-edge. For the
  values the edge captured, use `--sample-mode pre-edge`
  (`reference/sync-vs-async`).

## Examples

```bash
bwave sample sim.fst "*valid" 'h1 -s "*data*" -s "*addr*"
bwave sample sim.fst --virtual "hsk = *valid & *ready" hsk 'h1 -s "*data*"
bwave sample sim.fst "*err" rising --count
```

## Clock-grid behavior

- Literal triggers skip cycles inside a clock stop. `--include-stopped`
  matches them too.
- Edge and change triggers are unaffected.
- `--sample-mode pre-edge` reads both the trigger and the captured values
  (Virtual Signals included) just before each edge. Cycle numbers do not
  change.

See `reference/sync-vs-async` for the clock verdict and sampling modes.
