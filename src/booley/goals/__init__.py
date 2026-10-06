"""Goal Mode: Goals, Goal Records, and the Change Log (ADR 0067).

- :mod:`booley.goals.model` — Goal arguments, translated Goals, and the Goal
  Record values, with strict parsing of agent input and persisted records.
- :mod:`booley.goals.translate` — Goal arguments to Criteria, merging Goals for
  one key to the stricter Goal.
- :mod:`booley.goals.paths` — the Goal Record layout in the Project directory.
- :mod:`booley.goals.store` — worktree identity, locks, and revisioned
  ``record.json`` writes.
- :mod:`booley.goals.changes` — the append-only Change Log.

The package depends only on ``criteria``, ``runtime``, and ``core``. Its
vocabulary is in ``GLOSSARY.md`` beside this file.
"""
