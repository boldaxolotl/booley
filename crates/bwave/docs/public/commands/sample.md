# bwave sample

## Synopsis

```bash
bwave sample <FST_FILE> <TRIGGER_PAT> <TRIGGER_VAL>
             [-s PATTERN[%RADIX] ...]
             [--first | --last | --before N | --after N] [--count]
             [-t START:END]
             [--async] [--clock PAT] [--reset PAT] [--with-reset]
             [--virtual "name = expr"] [--marker NAME CYCLE]
             [--format text|json] [--limit N]
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
- `-s PATTERN` is optional but expected: without it
  you'll snapshot every signal in the store on every
  trigger fire, which is rarely what you want.
- Modifier flags (`--first`, `--last`, `--before`,
  `--after`, `--count`) mirror `find`.
- `--virtual` and `--marker` apply.

## Output shape

Text mode prints a snapshot block per trigger event:

```
# sample: tb.dut.valid == 'h1
# cycle 42:
   data        'h0000DEAD
   addr        'h0010
# cycle 187:
   data        'h0000BEEF
   addr        'h0014
# cycle 293:
   data        'h00C0FFEE
   addr        'h0018
# 3 trigger fires
```

With `--count`:

```
3
```

JSON mode is **not yet implemented** for `sample`. Text
mode only.

## Common errors

- **No snapshots emitted but `find` shows the trigger
  fires**: the `-s` pattern matched nothing. Sample
  blocks are skipped when there's nothing to capture.
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

Sample on rising edge of an error signal, only counting
events:

```bash
bwave sample sim.fst "*err" rising --count
```
