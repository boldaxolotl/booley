# bwave sample

## Synopsis

```bash
bwave sample <FST_FILE> <TRIGGER_PAT> <TRIGGER_VAL>
             [-s PATTERN[%RADIX] ...]
             [--first | --last | --before N | --after N] [--count]
             [-t START:END]
             [--async] [--clock PAT] [--reset PAT] [--with-reset]
             [--virtual "name = expr"]
             [--format text] [--limit N]
```

## Semantics

Each time `TRIGGER_PAT` equals `TRIGGER_VAL` (or matches
the named edge), capture a snapshot of the `-s` signals.
This answers the question "every time X fires, what was
Y?" It's useful for sampled-on-handshake debugging.

Think of it as `find` + a snapshot per match. The trigger
defines *when*; `-s` defines *what to capture*.

`TRIGGER_VAL` accepts the same forms as `find`: Verilog
literals, width-prefixed literals, and edge keywords
(`rising`, `falling`, `change`).

## Defaults and requiredness

- `<FST_FILE>`, `<TRIGGER_PAT>`, `<TRIGGER_VAL>` are
  required positional args.
- `-s PATTERN` optionally selects stored and Virtual Signal
  capture rows. Without it, every stored and virtual row is
  captured on every trigger fire, which is rarely what you
  want. Unselected virtual helpers remain available to the
  trigger and to selected virtual rows.
- Modifier flags (`--first`, `--last`, `--before`,
  `--after`, `--count`) mirror `find`.
- `--virtual` applies.

## Output shape

Text mode prints time/name/value rows per trigger event:

```
1000 c 1
```

With `--count`:

```
1
```

`--format json` exits with code 2: `JSON output is not implemented for sample; use find/value/stats/list`.

## Common errors

- **No snapshots emitted but `find` shows the trigger
  fires**: the `-s` pattern matched nothing. Sample
  exits with code 2 for unmatched watched selectors, except
  `--count` needs no watched rows.
- **`--first and --last are mutually exclusive`**: same
  rule as `find`.
- **Captured data looks stale**: sync mode samples on
  the same edge the trigger fires, post-edge. If you want
  the value the trigger was reacting to (pre-edge),
  sample one cycle earlier with `--before <trigger+1>` or
  use async mode.

## Examples

Sample `data` and `addr` every time `valid` fires:

```bash
bwave sample sim.fst "*valid" 'h1 -s "*data*" -s "*addr*"
```

First handshake only:

```bash
bwave sample sim.fst "*valid" 'h1 \
    -s "*data*" -s "*addr*" --first
```

Sample on a virtual handshake signal:

```bash
bwave sample sim.fst \
    --virtual "hsk = *valid & *ready" \
    hsk 'h1 -s "*data*"
```

Capture a composed Virtual Signal without printing its helper:

```bash
bwave sample sim.fst "*clk" rising \
    --virtual "valid = *req & *ready" \
    --virtual "stalled = valid & *busy" \
    -s "stalled"
```

Sample on rising edge of an error signal, only counting
events:

```bash
bwave sample sim.fst "*err" rising --count
```
