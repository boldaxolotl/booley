"""Goal Mode: Goals, Goal Records, and the Change Log (ADR 0067).

- :mod:`booley.goals.model` — Goal arguments, translated Goals, and the Goal
  Record values, with strict parsing of agent input and persisted records.
- :mod:`booley.goals.translate` — Goal arguments to Criteria, merging Goals for
  one key to the stricter Goal.
- :mod:`booley.goals.paths` — the Goal Record layout in the Project directory.
- :mod:`booley.goals.store` — worktree identity, locks, and revisioned
  ``record.json`` writes.
- :mod:`booley.goals.changes` — the append-only Change Log.
- :mod:`booley.goals.goalsets` — rendering and create-only seeding of the
  Project-owned Goalsets.
- :mod:`booley.goals.entry` — the locked, re-drivable entry transaction and
  its rollback.
- :mod:`booley.goals.checkout` — the Git operations entry performs.
- :mod:`booley.goals.protected_inputs` — the protected-input resolver
  contract, digest, and violation check.
- :mod:`booley.goals.rules` — the rules text an agent follows in Goal Mode.
- :mod:`booley.goals.preview` — the switch that registers the Goal Mode
  surface before it replaces Ticket Mode.

The package depends on ``criteria``, ``evidence``, ``targets``, ``runtime``,
``core``, and the Flow-neutral ``flows.baseline_pins``; concrete Flows are
composed in by the MCP entry point. Its vocabulary is in ``GLOSSARY.md``
beside this file.
"""
