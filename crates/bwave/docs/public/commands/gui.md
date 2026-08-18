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

# 2. Show that neighborhood: these signals, this time window
bwave gui @dut --signals 'tb.dut.fifo.*' --time 1180c:1260c

# 3. Follow-ups: add signals to the same view, point at the moment
bwave gui @dut --signals 'tb.dut.ctrl.state' --append --cursor 1200c
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

## `--signals`

Repeatable, and takes the same globs as `-s` (but no `%RADIX`). Expansion is
capped at 64 signals (`--max-signals`) and **errors** past the cap — narrow the
glob, don't raise the cap.

The signal list `gui` prints is read back from the viewer, so it is what the
human actually sees. A signal missing from the viewer's netlist is dropped and
named in a `WARNING` on stderr — tell the human what did not make it onto the
screen instead of claiming the full view. The trace itself is fine;
`signal` / `find` / `value` still answer for that signal.

## Transport

A scoped `gui` drives the VaporView viewer in the user's VS Code window over its
WCP control server, and **hard-errors if that server is off** — surface the setup
hint to the human; it never silently degrades.

A bare `bwave gui [@alias]` just opens the trace, falling back to the editor CLI
if the control server is unreachable.
