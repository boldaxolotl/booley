Show a trace in the Waveform Viewer (VaporView in VS Code).

`gui` is for the HUMAN — it puts a waveform on their screen. It is not how an
agent reads values: `signal` / `find` / `value` / `stats` answer those, and they
need no viewer running. Reach for `gui` when the human needs to SEE something:
an FSM walking its states, a handshake, a bus settling.

## Query first, then show what you found

Don't open the bare trace. Locate the event, then show a scoped view of exactly
that neighborhood.

```bash
# 1. Locate the event
bwave find @dut "tb.dut.fifo.overflow" rising --first

# 2. Show that neighborhood as a named, collapsible section
bwave gui @dut --group 'FIFO handshake=tb.dut.fifo.*%h@green' --time 1180c:1260c

# 3. Follow-ups: add another logical section, point at the moment
bwave gui @dut --group 'Control=tb.dut.ctrl.*' --append --cursor 1200c
```

## The clock row

A new view (the default, replace mode) gets the trace's **clock as row 1**
automatically — the same clock the cycle counts are measured against. A waveform
without its clock is unreadable: you cannot tell a cycle from a glitch.
`--append` does not re-add it (the view already has one); `--no-clock` opts out.

## Markers: `--time` and `--cursor`

`--time START:END` does more than move the viewport. It drops VaporView's two
markers on the ends of the range (main=START, alt=END), so the viewer's status
bar reports the span as a delta and the human reads the duration off the screen
instead of subtracting ruler numbers.

`--cursor T` moves the START (main) marker somewhere else inside the range; END
stays put.

Both take the same time tokens as `-t`, including marker names — so a marker set
during the query can bound the view:

```bash
bwave gui @dut --time overflow_start:overflow_end
```

## Native groups: `--group NAME=GLOB[%RADIX][@COLOR]`

For a view with more than one logical role, use repeatable
`--group 'NAME=GLOB'`. Give each group a short semantic name from the current
debugging question—`Request path`, `Arbitration`, `Backpressure`, `Response`
rather than merely repeating a module prefix. VaporView displays each as a
native named row whose signals can be collapsed.

Repeat a name to add more than one pattern to that group:

```bash
bwave gui @dut \
  --group 'Request path=tb.dut.req_*' \
  --group 'Request path=tb.dut.inflight*' \
  --group 'Response=tb.dut.resp_*'
```

If patterns overlap, the first named group owns the signal. A named group also
takes ownership over the same row requested through `--signals`, so one signal
is never duplicated merely to satisfy overlapping selectors.

In replace mode the requested groups become the new view. With `--append`, a
same-name group is extended and a new name creates another group. Existing
collapse state and signal formatting are preserved. `gui` reads the hierarchy
back from VaporView before reporting success. Replace mode stages additions,
commits one exact ordered tree, and restores the previous tree if the commit or
readback fails. Missing grouped-layout support or any mismatch in membership,
placement, order, radix, or color is an error, never a silently altered view.

## Top-level rows: `--signals`

Repeatable, and takes the same globs as `-s`, plus the presentation suffixes
described below. Combined
`--signals` and `--group` expansion is capped at 64 signals (`--max-signals`)
and **errors** past the cap — narrow the glob, don't raise the cap. Use this for
the few signals that intentionally belong at the top level; prefer named groups
for a multi-section view.

## Radix and color suffixes

Both `--signals` and the glob side of `--group` accept
`GLOB[%RADIX][@COLOR]`. Radixes are `%b` (binary), `%h` (hexadecimal), and `%d`
(unsigned decimal). Colors are `@red`, `@blue`, and `@green`; when both are
present the radix comes first:

```bash
bwave gui @dut \
  --signals 'tb.clk%b@red' \
  --group 'State=tb.dut.state%h@blue' \
  --group 'Datapath=tb.dut.result%h@green'
```

An explicit suffix updates an already displayed row under `--append`; omitted
properties preserve its current presentation. The CLI applies these properties
through VaporView's recursive layout and reads them back before reporting
success.

The signal list `gui` prints is read back from the viewer, so it is what the
human actually sees. A signal missing from the viewer's netlist is dropped and
named in a `WARNING` on stderr — tell the human what did not make it onto the
screen instead of claiming the full view. The trace itself is fine;
`signal` / `find` / `value` still answer for that signal.

## Transport

A scoped `gui` drives the VaporView viewer in the user's VS Code window over its
WCP control server, and **hard-errors if that server is off** — surface the setup
hint to the human; it never silently degrades.

Groups and explicit radix/color require Booley's VaporView compatibility patch. If the CLI reports that
`set_signal_layout` or `get_signal_layout` is missing, run
`python -m booley.runtime.incontainer_vaporview`, reload the VS Code window, and
retry.

A bare `bwave gui [@alias]` just opens the trace, falling back to the editor CLI
if the control server is unreachable.
