# gui

Booley has no waveform viewer for now, so `gui` shows nothing. It resolves its
target and its selection, reports any error in them, and otherwise exits
non-zero with a message that says there is no viewer. `gui` is a command of the
Booley wrapper, not of the native binary; `bwave gui --help` lists the selection
flags it checks.

To read values, use `signal` / `find` / `value` / `stats`. They need no viewer.
