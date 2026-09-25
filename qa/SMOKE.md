# Release smoke list

Ten must-pass items for a release candidate, run against the pinned PicoRV32
demo Project from the picorv32 mission. Record a table in `log.md`:

| # | Item | Result | Finding |
|---|---|---|---|

Result is `pass`, `fail`, or `not-run`. An item passes only when it works as the
documentation describes, with no workaround; a workaround makes it `fail` with a
finding. The smoke verdict is `PASS` only when all ten pass; write it as the
first line of the report.

1. The candidate wheel installs into a clean venv and `booley --version` names
   the expected version and commit.
2. `booley bootstrap --check-only` reports ready after `booley bootstrap`.
3. `booley init` on a fresh clone of the demo Project succeeds.
4. `booley doctor` reports no FAIL.
5. The baseline simulation Target passes.
6. Lint runs and reports its result.
7. Synthesis runs and reports area and timing.
8. `bwave` lists signals from a trace the simulation produced.
9. One Ticket goes from create through `booley run` to merge.
10. Booley's cleanup leaves no orphan containers, worktrees, or branches.
