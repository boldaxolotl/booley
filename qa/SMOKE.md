# Release smoke list

The Release Smoke List: ten must-pass items for a release candidate. The
operator walks it when `booley-qa-run` is given `smoke`, before the mission,
against the pinned PicoRV32 demo Project from the picorv32 mission. Unlike a
mission it is not a bug hunt: each item passes or fails. Record a table in
`log.md`:

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
9. One Metric-Driven Development round trip succeeds: commit
   [`missions/picorv32/fixtures/metrics.toml`](missions/picorv32/fixtures/metrics.toml)
   as `metrics.toml` in the Booley Directory's own repository (`git -C .booley
   commit`; the demo is Stealth); `booley measure --validate` lists no error;
   in a new worktree (`booley worktree new`) commit a harmless change and run
   `booley measure`: exit 0, green header, the commit Measured; read the
   Measurement Report; run it again: it prints the stored run and starts no
   Flow.
10. Booley's cleanup leaves no orphan containers, worktrees, or branches.
