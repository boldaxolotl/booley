# gui

Check a waveform selection. Booley has no waveform viewer for now, so `gui`
shows nothing: it resolves its target and its selection, reports any error in
them, and then exits with a non-zero status and a message that says there is no
viewer. `gui` is a command of the Booley wrapper, not of the native binary.

To read values, use `signal` / `find` / `value` / `stats`. They need no viewer.

## Target

The target resolves like a query: `@ALIAS` uses a registered session, an
explicit path is used as given, and no argument means the latest registered
trace. A sim directory works as it does for `register`. A missing alias, a
missing path or a legacy `.bwave` cache is reported before the no-viewer
message.

## Selection

Every selection flag is checked and reports the same errors it always did.

| Flag | Takes | Checked |
|---|---|---|
| `--signals GLOB[%RADIX][@COLOR]` | repeatable glob, as `-s` | the glob matches; the radix and color are known |
| `--group NAME=GLOB[%RADIX][@COLOR]` | repeatable named group | the name, the glob and its suffixes |
| `--time START:END` | range, as `-t` | both ends lie inside the trace |
| `--cursor T` | one time token | `T` lies inside the trace |
| `--append`, `--no-clock` | flags | `--append` needs something to add |
| `--max-signals N` | a number | the combined expansion stays under the cap (64) |

Radixes are `%b`, `%h` and `%d`; colors are `@red`, `@blue` and `@green`, and
the radix comes first. Time tokens include marker names set with `bwave
markers`. A `--time` end past the trace is clamped to the trace end, with a note
on stderr. A `--signals` glob that matches nothing is named in a `WARNING`; when
every `--signals` glob misses, or a `--group` glob matches nothing, the command
fails and the error carries `did you mean '<full-path glob>'?` when the glob
matches below another scope.

A selection error exits with status 2. With a valid selection, or with none,
the exit status is 1 and the no-viewer message is the last line on stderr.
