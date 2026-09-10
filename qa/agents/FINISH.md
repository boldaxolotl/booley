# Finish a Scenario Run

Read [Record](RECORD.md). Enter this stage after execution, at the Scenario's cleanup
reserve boundary, or whenever authority, evidence integrity, or resource control is
lost. Stop new product work and preserve completed records.

## Reconcile resources

First quiesce every active, privileged, scarce, or externally visible run-owned
resource. This includes processes, Session Runtimes, mounts, listeners, relays,
leases, Grants, License Profiles, temporary credentials, and registrations. Prove
their release without exposing secrets or changing borrowed state.

An owned resource may be `retained-for-review` only when evidence proves all of these:

- it is inert and has no process, Session Runtime, mount, listener, relay, lease,
  Grant, License Profile, credential, executable hook, or other live authority;
- it consumes no scarce quota and contains no secret material;
- its exact identity, bounded storage location, review purpose, Human Maintainer
  owner, and later deletion instructions are recorded; and
- every claim that depends on it also has immutable evidence outside that mutable
  state.

Unknown or ineligible state must be quiesced, not retained. Workspaces, worktrees,
branches, repositories, Projects, Targets, projections, volumes, and Runtime Images
are eligible only when the predicate above is proven; their type alone does not make
them inert. Preserve borrowed resources unchanged.

Record one ledger disposition for every resource: `released`, `retained-for-review`,
`borrowed-preserved`, or `release-failed`. Intentional retention does not prevent a
pass. `release-failed` or missing evidence for mandatory quiescence does.

Scenario Checks that exercise product cleanup behavior remain authoritative: execute
their declared deletion or preservation stimulus even when the final workspace would
otherwise be retained. Archive its evidence first when the Scenario requires it.

## Finalize the run

If the operator was interrupted, preserve its partial record and use this stage only
to reconcile resources and append missing Check Results as `blocked`. Start a new
Scenario Run for further product work. Context compaction within the same live
operator is not an interruption and follows the dispatcher resume rule.

Finish within the original deadline when possible. If time expires, record the
overrun and continue mandatory quiescence without extending the run or claiming timely
completion.

Append `blocked` Check Results for selected Checks without one. Report execution
status as `completed`, `deadline reached`, or `operator error`, and quiescence status
as `complete` or `failed`. Calculate the Scenario Run Outcome and aggregate
Qualification under [Qualification](../user/QUALIFICATION.md). Product failure keeps
precedence over missing work or failed quiescence.

The run is complete when every selected Check has a Check Result, each ledger resource
has a disposition, mandatory quiescence has evidence, retained review state satisfies
the retention predicate, and `summary.md` reports the Scenario Run Outcome,
Qualification where applicable, execution status, quiescence status, and retained
locations. Set `operator-state.json` to `complete` only then.

Follow the applicable Scenario assets for the [Taxi submodule
companion](../scenarios/taxi/fixtures/submodules.md) and the D-01/D-02 Booley Feedback
probes. Those assets own their fixture, evidence, authority, and credit boundaries.
