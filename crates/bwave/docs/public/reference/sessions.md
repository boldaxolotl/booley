# Sessions

A session is a name, `@<alias>`, that points at a registered trace, so
queries need no file path. Sessions belong to the Booley `bwave` wrapper;
the native binary only takes a store path.

## Using a session

The alias comes **before** the subcommand:

```bash
bwave @dut list -s "*state*"
bwave @dut value --at 500 -s "tb.dut.*"
bwave list                 # no alias: the default session
```

The wrapper only resolves an alias in that first position. `gui` and
`markers` are wrapper commands and take the alias after their name:
`bwave gui @dut`, `bwave markers @dut set err_start 1234`.

## Where sessions come from

- A traced `booley flow sim` run registers each traced Target as
  `@<target>`, and the last one as the default session.
- `bwave register PATH --as ALIAS` registers a store or a sim directory by
  hand. A sim directory's `trace.vcd` is built into an `.fst` store for
  you; a `.vcd` file passed directly needs `--build`.

Each checkout, main or worktree, keeps its own sessions, and a new worktree
starts with none. Outside a Project, sessions live in the temporary
directory. The user-facing details are in the Sim Flow docs
(`FLOW-SIM.md`).

Markers set with `bwave markers` are stored per session; see
`reference/markers`.
