# bwave stuck

## Semantics

Report signals that never transitioned during the
simulation (after reset, unless `--with-reset` is set).
Stuck signals are usually one of:

- Tied-off inputs (legitimate constants).
- Disconnected wires (synthesis hazard).
- Dead control paths (FSM states never reached).
- A bug: the signal should have toggled but didn't.

`[VALUE]` is a Verilog literal (`reference/verilog-literals`);
with it, only signals stuck at that value are reported.

## Output shape

Text mode, one signal per row, with the constant value:

```
# Stuck signals: 1 of 1 analyzed
  constant  4-bit  stuck at A  (100%, 100ns)
```

With an explicit `VALUE` filter:

```
# Stuck signals: 1 of 1 analyzed (filter: A)
  constant  4-bit  stuck at A  (100%, 100ns)
```

`--format json` exits with code 2: `JSON output is not implemented for stuck; use find/value/stats/list/signal/wave/distance/overview`.

## Common errors

- **Signals that toggle only during reset appear stuck**:
  `stuck` scans the post-reset window by default. Add
  `--with-reset` to include the reset window.

## Examples

```bash
bwave stuck sim.fst
bwave stuck sim.fst 'h1
bwave stuck sim.fst 'hX
```
