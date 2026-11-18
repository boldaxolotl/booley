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
  `tb.dut.o_data`. Anchor it (`tb.dut.o_data`) when you know the scope. When a
  bare name matches in two or more scopes, the query prints a `# NOTE:`
  listing up to three of them (JSON: `warnings`).
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

## Bit ranges and indexes

A trailing `[N]` or `[M:N]` selects by name first: `dmem_wr[0]` is the
array element `dmem_wr[0]`, and `counter[7:0]` is the signal listed under
that name.

On a vector, a range selects those bits. With an 8-bit `qa.data`,
`-s "qa.data[5:4]"` is a 2-bit signal named `qa.data[5:4]`, and
`find "qa.data[5:4]" "2'b11"` compares only those bits. `qa.data[3]` is bit
3, and `[7:0]` is the whole signal. Bits are numbered from 0, or by the
vector's own range: `[3:2]` on `addr[31:2]` is its two lowest bits.

- A range outside the vector, or reversed (`[4:5]`), exits 2 and names the
  vector's width. A glob slices every vector it matches, so a vector too
  narrow for the range is the same error.
- A partial range on an ascending vector (`v[0:3]`) exits 2.
- Radix suffixes work: `-s "qa.data[5:4]%b"`.
- `--clock`, `--reset` and `bwave gui` take whole signals, so a slice exits 2.
- `--virtual` has its own bit-slice grammar.

## Display radix suffix

Append `%h` (default), `%d` or `%b` to a pattern, with no space, to choose
how text output renders its values: `-s "state%d"`. Quote it for the shell.
The suffix only changes rendering: JSON output and value matching ignore
it. Any other suffix exits 2.

## Misses

- Every `-s` pattern matches nothing: exit 2 with
  `ERROR: no signals match pattern(s) ...`; a bare name adds a reminder that
  it matches only the end of the full path.
- Some match, some don't: the query runs, and each miss prints
  `# WARNING: no signals match '<pat>' — filter dropped`. That signal's
  rows are missing from the result. If `bwave list` cannot find it either,
  the simulator never dumped it; unpacked arrays and memories are often
  absent. One more line says rows for these patterns are missing.
