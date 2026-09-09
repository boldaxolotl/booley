# Scenario Protocol

Applies to discovery and qualification runs alike. Both execute the same selected
checks and preserve unexpected observations. Run purpose is descriptive metadata.

## Prepare

The coordinator assigns actual setup, development, evaluation, diagnostic, and
cleanup work to delegates. It owns sequencing, evidence integration, and the report.
Record delegate identity and assignment with the step; no delegation event system
is required. Delegates cannot grant authority or change acceptance requirements.

Before product exercises, freeze a run ID, selected profile, exact published Booley
release and package identity, release documentation, suite commit, pinned IP inputs,
native-host OS and architecture, provider, Runtime Attachment, agent backend,
relevant Session Image and EDA tool identities, deadline, artifact root, pre-run
capability probes, and granted authority in `run.json`. Record initial identities
there; Session Image identities created by Project Setup and later Git repository or
accepted-commit identities belong in the producing step's result and evidence.
Missing required initial identity blocks execution. Missing authority means denied.

Runs are unattended. Authority covers only the declared actions and owned resources.
Use published Booley documentation, packaged skills, CLI/MCP help, and ordinary
Project inspection. Consult Booley source for verification/classification only after
capturing the original observation. Use exact published releases; local wheels,
editable installs, development builds, and imports from a source checkout are excluded.

## Execute

Read each step's action, prerequisites, checks, and recovery instructions together.
A check declares its stimulus, expected observation, contract source, and evidence.
Preserve artifact identity and freshness. Reuse one artifact for multiple checks only
when it independently supports each claim. Delegate prose cannot replace artifacts.
Runtime Attachment claims require evidence from the attached application. Waveform
Viewer claims require timestamped visual evidence from a qualified observer.

Evidence requirements retain the existing minimums: discovery uses identity-bound
output and meaningful input rejection where supported; Booley Flow and EDA tool
checks retain the actual normalized grade and verified fresh artifacts with Target,
Booley Flow, and EDA tool identity; stateful checks prove relevant transitions,
failure, recovery or persistence, and cleanup; Interactive Mode and Ticket Mode
checks correlate Runtime Attachment and agent-backend identity with durable logs and
artifacts; documentation checks identify the consulted revision and observed
behavior. Console text is authoritative when the diagnostic text itself is the
contract. Preserve Booley's underlying grades and artifact meanings in the evidence.

Delegates retain diagnostic and implementation freedom allowed by the journey. Exact
commands or prose are mandatory only where the design says their literal form is
under test. Apply the journey's existing timeouts and narrow retry allowances.
Prerequisites may reference earlier checks; an unexpected failure blocks dependent
work until the required state is demonstrably restored. Continue independent work
while authority, evidence, and resources remain controlled.

A seeded fault must prove baseline success, inject the declared fault, observe the
expected failure, restore state, and prove recovery. Detecting that expected failure
passes its negative check. An unexpected product failure is retained even after a
workaround or successful retry. Attach post-recovery observations to the original
result; they never retroactively convert it to pass.

Record alternatives and deviations with the affected check. An explicitly permitted
alternative may satisfy the check. Undeclared changes to inputs, actions, authority,
or evidence block the affected claim unless trustworthy failure evidence already
exists. Capture that failure regardless of the invalidated claim.

## Record results

Use the files and minimum fields in [Format](FORMAT.md). Check outcomes are:

| Outcome | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | A selected required check lacks trustworthy evidence, including failed prerequisites, timeout, infrastructure/operator error, or invalid execution |
| `unavailable` | A pre-run probe proved an applicable declared capability absent |

A capability lost after declaration is fail or blocked, never retrospectively
unavailable. Product-inapplicable checks are excluded explicitly by the profile;
they are not passes. A selected check with no result is blocked at finalization.

Append observations and corrections to `results.jsonl`; corrections identify the
record and evidence of the recording mistake. Correcting a recording mistake does
not authorize erasing a real failure. Preserve every trustworthy failed attempt.
Conflicting trustworthy results produce a flaky Finding and prevent qualification.

Capture Findings, Friction Reports, Impressions, and wins with stable source IDs,
original text, kind, time, classification, step/check links, and evidence. Defects
also need expected/observed behavior, stimulus, and reproduction information. Append
status updates without rewriting original observations. Passed checks need not be
duplicated as wins. Preserve unclassified observations for downstream triage.

Retain unredacted internal QA records for Consolidate Findings, excluding secret
values. Do not invoke Booley Feedback or submit reports externally during a run.

## Recover and finish

Keep the journey's named phase recovery points with saved source identities and
artifact references. They support bounded recovery within a live run. General
restart/resume of an interrupted coordinator is deferred: preserve its partial
record, reconcile owned resources, and start a new run. Old results do not satisfy
required checks in that new run.

Persist ownership and intended disposition in `resources.json` before creating a
resource where its identity is known, otherwise immediately upon acquiring it and
before dependent work. Track branches, worktrees, processes, Session Runtimes,
Session Images, mounts, registrations, Grants, profiles, and relays as applicable. Ownership must
be specific enough for cleanup after an interrupted run without touching others'
state. Update the ledger as resources are released; retain cleanup result evidence.

Start cleanup by the journey's declared reserve boundary, before its absolute
deadline. Stop new work, retain results, and complete cleanup within that deadline.
If the deadline is nevertheless exceeded, preserve the overrun and attempt remaining
cleanup; never extend the run or claim timely completion. Stop
earlier if authority, evidence integrity, or resource control is lost, or no runnable
independent work remains. Cleanup runs after success, failure, or interruption.
Preserve borrowed installations, credentials, caches, and other pre-existing state.

Finalize missing checks as blocked, list owned-resource disposition, and report
operational completion as `completed`, `deadline reached`, or `operator error`.
Calculate qualification using [Qualification](QUALIFICATION.md). Incomplete
mandatory cleanup prevents a pass without concealing trustworthy product failures.

Taxi additionally owns the explicitly authorized [disposable submodule companion](scenarios/taxi/fixtures/submodules.md).
Construct it separately from the authentic pinned Taxi checkout; preserve both
required RTL dependencies and the prescribed warm/cold failure and restoration
observations. Fixture-construction tests and root-only synthesis are not functional
Simulation Flow credit.

The inventory's D-01/D-02 probes exercise Booley Feedback using disposable file-only
fixtures. Their outputs are scenario evidence, not the suite's reporting channel.
They authorize no submission, email or issue. Keep suite results/findings separate
and directly usable by Consolidate Findings.
