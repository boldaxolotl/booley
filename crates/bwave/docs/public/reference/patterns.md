# Signal patterns

How `-s PATTERN` (and `--clock`, `--reset`, `gui --signals`/`--group`)
picks signals. Read this when a pattern matches too much, too little, or
nothing. Virtual Signal definitions have their own grammar:
`reference/virtual-signals`.

## Bare names match a suffix

A pattern with no `*`, `?` or `[` matches the end of the full hierarchical
name, as if it started with `*`. Matching is case-sensitive.

- `dmem_addr` matches `tb.dut.dmem_addr`, but not `tb.dut.dmem_addr_next`.
- The suffix need not start at a scope boundary: `bit` matches
  `tb.dut.state_bit`.
- A bare name matches at every depth, so `o_data` returns both `o_data` and
  `tb.dut.o_data`. Anchor it (`tb.dut.o_data`) when you know the scope.
- A trailing bit range is optional: `counter` matches `counter[7:0]`.

## Globs match a whole path

A pattern containing `*`, `?` or `[` is a glob. It matches either the full
path or the path relative to the store's common scope, the `# scope:` line
an unfiltered `bwave list` prints. With `# scope: tb`:

- `tb.dut.core.*` and `dut.core.*` both match everything under
  `tb.dut.core`, at any depth.
- `core.*` matches nothing: it is anchored at `tb`, not at any depth. The
  error suggests the full-path form: `did you mean 'tb.dut.core.*'?`.
- `*dmem*addr*` matches a substring anywhere.

A narrowed `list -s` prints a deeper `# scope:`, which is not the anchor.

A trailing all-digit index such as `[3]` or `[7:0]` is literal, not a glob
character class. `mem[0-3]` is still a class.

## Display radix suffix

Append `%h` (default), `%d` or `%b` to a pattern, with no space, to choose
how text output renders its values: `-s "state%d"`. Quote it for the shell.
The suffix only changes rendering: JSON output and value matching ignore
it. Any other suffix exits 2.

## Misses

- Every `-s` pattern matches nothing: exit 2 with
  `ERROR: no signals match pattern(s) ...`.
- Some match, some don't: the query runs, and each miss prints
  `# WARNING: no signals match '<pat>' — filter dropped`. That signal's
  rows are missing from the result. If `bwave list` cannot find it either,
  the simulator never dumped it; unpacked arrays and memories are often
  absent.
