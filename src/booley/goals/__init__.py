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
- :mod:`booley.goals.binding` — the immutable binding of one run to the
  worktree's active Goal Mode.
- :mod:`booley.goals.publication` — the gate every Goal evidence write passes.
- :mod:`booley.goals.state_store` — fail-closed loads and merging saves of
  ``booley_state.json``.
- :mod:`booley.goals.recorder` — Goal evidence in the Criterion evidence
  ledger: the identity codec, projection fencing, and the recorder.
- :mod:`booley.goals.freshness`, :mod:`booley.goals.target_surface`, and
  :mod:`booley.goals.simulation` — when met evidence is stale, the Target
  declaration digest, and the simulation suite contract.
- :mod:`booley.goals.flow_execution` — the Flow execution adapter of a bound
  Goal run.

The package depends on ``criteria``, ``evidence``, ``targets``, ``fusesoc``,
``config``, ``runtime``, ``core``, and the Flow-neutral ``flows`` modules
(``baseline_pins``, ``execution_persistence``, ``request``,
``source_fingerprint``, ``target_test_suite``); concrete Flows are composed in
by the MCP entry point. Its vocabulary is in ``GLOSSARY.md``
beside this file.
"""
