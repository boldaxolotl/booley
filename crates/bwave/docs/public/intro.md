# B-Wave

B-Wave is a command-line tool for querying RTL simulation waveforms. `bwave
build` converts a VCD from any simulator into an `.fst` waveform store once;
every query then reads that store instead of re-parsing the VCD. The store is
a plain FST file, so GTKWave and VaporView open it too. Read this page for a
map of the docs; each topic below is one `bwave docs show <topic>` away.

## Start here

- `commands/overview`: every subcommand, the options they share, and the
  file-input rule.
- `reference/sync-vs-async`: how cycles are numbered, and when to use raw
  timestamps. Read it before trusting a cycle number.
- `commands/wave`: the usual first query, signals against cycles.

## Reference

- `reference/patterns`: how `-s` patterns select signals.
- `reference/time-tokens`: `-t`, `--at` and other time arguments.
- `reference/verilog-literals`: values for `find`, `sample`, `stuck`, and
  edge keywords.
- `reference/virtual-signals`: `--virtual` predicates.
- `reference/markers`: named times in `wave` and the wrapper.
- `reference/sessions`: `@<alias>` sessions in the Booley wrapper.

## Troubleshooting

- `troubleshooting/empty-results`: query returned nothing.
- `troubleshooting/reset-skipping`: output starts later than expected.
- `troubleshooting/clock-detection`: wrong clock picked.

`bwave docs topics` lists every topic and `bwave docs search <text>`
searches them. `bwave <command> --help` is authoritative for flags, and
`bwave schema` for `--format json` output.
