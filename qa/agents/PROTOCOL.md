# Scenario protocol

This protocol applies to discovery and qualification Scenario Runs. Both execute the
same selected Checks and preserve unexpected observations; run purpose is descriptive
metadata.

## Admit the run

You are the Scenario Operator. Require:

- a Scenario ID and Configured Scenario ID from the production `scenario.yaml`;
- a writable artifact root outside disposable Project state; and
- credentials and licensed EDA access required by the Configured Scenario.

Stop before product work if an input is missing. Keep secrets out of prompts and run
records; obtain them through approved provider and EDA mechanisms. Pre-run probes may
mark capabilities unavailable but cannot change the Configured Scenario's scope.

Explicit skill invocation grants authority for the resources and mutations declared
by the selected Scenario. Any action outside that scope requires explicit user
authority. Runs are unattended once admitted.

## Prepare

Read the selected production `scenario.yaml`, resolve the Configured Scenario's named
`check_sets` in order, and read [Format](FORMAT.md). The Scenario owns parameters,
actions, evidence, authority limits, budgets, recovery, and cleanup.

Run `python qa/validate.py` from the repository root. Stop if validation fails; it
validates the suite structure without executing QA.

Generate a fresh Scenario Run ID that does not collide beneath the artifact root; the
Configured Scenario ID is not the Scenario Run ID. Create the run directory. Before
product exercises, write `run.json` with both IDs, declared parameters, exact immutable
Booley product revision and artifact or package identity, matching documentation
snapshot, suite commit, pinned IP inputs, native-host OS and architecture, provider,
Runtime Attachment, agent backend, relevant Session Image and EDA tool identities,
deadline, artifact root, pre-run evidence, and granted authority.

Record initial identities in `run.json`; record Session Images created during Project
Setup and later repository or accepted-commit identities in the producing Step's
result and evidence. A missing required initial identity blocks execution.

Use documentation and packaged skills matching the tested build, CLI or MCP help, and
ordinary Project inspection. Consult source only to verify or classify behavior after
capturing the original observation. Use the artifact form declared by the Configured
Scenario. Published releases and unreleased candidates are allowed. Candidate
packages, including local wheels, must be immutable artifacts bound to a source commit
and content hash. Exclude undeclared substitutions, floating references, editable
installs, and execution or imports from a source checkout.

## Execute

You may assign setup, development, evaluation, diagnostics, and cleanup to sub-agents,
but retain responsibility for sequencing, evidence, and the report. Record each
assignment and sub-agent identity with its Step; do not create a separate delegation
event system. Sub-agents cannot grant authority or change acceptance requirements.
They retain the diagnostic and implementation freedom the Scenario allows. Literal
commands or prose are mandatory only when their form is under test.

Execute selected Checks in Scenario order. Read each Step's action, prerequisites,
Checks, and recovery instructions together. Each Check declares its stimulus,
expectation, contract source, and evidence. Preserve artifact identity and freshness.
Reuse an artifact only when it supports every linked claim independently. Agent prose
cannot replace artifacts. Runtime Attachment claims require evidence from the attached
application; Waveform Viewer claims require timestamped visual evidence from a
qualified observer.

Discovery evidence includes identity-bound output and meaningful input rejection when
supported. Booley Flow and EDA tool evidence retains the actual normalized grade and
verified fresh artifacts with Target, Booley Flow, and EDA tool identities. Stateful
Checks prove relevant transitions, failure, recovery or persistence, and cleanup.
Interactive Mode and Ticket Mode Checks correlate Runtime Attachment and agent-backend
identities with durable logs and artifacts. Documentation Checks identify the
consulted revision and observed behavior. Diagnostic console text is authoritative
when it is the contract. Preserve underlying grades and artifact meanings.

Apply the Scenario's timeouts and narrow retry allowances. Prerequisites may name
earlier Checks. A failed prerequisite blocks dependent work until the required state
is demonstrably restored; continue independent work while authority, evidence, and
resources remain controlled.

A seeded fault must prove baseline success, inject the declared fault, observe the
expected failure, restore state, and prove recovery. The expected failure passes its
negative Check. Preserve unexpected product failures after workarounds or retries.
Link post-recovery observations to the original result; they do not turn it into a
pass.

Record alternatives and deviations with the affected Check. A declared alternative
may satisfy it. An undeclared change to inputs, actions, authority, or evidence blocks
the claim unless trustworthy failure evidence already exists; preserve that failure.

## Record

Use the files and fields in [Format](FORMAT.md). Append each Check attempt to
`results.jsonl` immediately:

| Outcome | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | A selected required Check lacks trustworthy evidence because of a failed prerequisite, timeout, infrastructure or operator error, or invalid execution |
| `unavailable` | A pre-run probe proved an applicable declared capability absent |

A capability lost after declaration is `fail` or `blocked`, not `unavailable`.
Explicitly exclude product-inapplicable Checks through the Configured Scenario;
exclusions are not passes.

Append corrections to `results.jsonl`. A correction identifies the mistaken record
and evidence of the recording error; it cannot erase a real failure. Preserve every
trustworthy failed attempt. Conflicting trustworthy results produce a flaky Finding
and prevent Qualification.

Append Findings, Friction Reports, Impressions, and wins to `findings.jsonl` with
stable source IDs, original text, kind, time, classification, Step and Check links,
and evidence. Defects also require expected and observed behavior, stimulus, and
reproduction details. Append status updates without rewriting observations. Passed
Checks need not also be wins. Preserve unclassified observations for downstream
triage.

Retain internal QA records unredacted except for secret values and directly usable by
Consolidate Findings. Do not use Booley Feedback as the suite's reporting channel or
submit reports externally during a run.

## Recover and finish

Keep the Scenario's phase recovery points with source identities and artifact
references for bounded recovery within the live run. Do not resume an interrupted
Scenario Operator. Preserve its partial record, reconcile its resources, and start a
new Scenario Run; old results do not satisfy the new run.

Before creating an owned resource, record its identity, ownership, and intended
disposition in `cleanup-ledger.json`. If its identity is unknown, record it after
acquisition and before dependent work. Track branches, worktrees, processes, Session
Runtimes, Session Images, mounts, registrations, Grants, License Profiles, and relays
as needed. Identify them well enough to clean up an interrupted run without touching
other state. Update released resources and retain cleanup evidence.

Start cleanup at the Scenario's reserve boundary before its deadline. Stop new work,
retain results, and finish cleanup by the deadline. If time expires, record the overrun
and continue cleanup without extending the run or claiming timely completion. Stop
earlier if authority, evidence integrity, or resource control is lost, or no independent
work remains. Clean up after success, failure, or interruption. Preserve borrowed
installations, credentials, caches, and other pre-existing state.

Finalize selected Checks without results as `blocked`, list resource disposition, and
report execution status as `completed`, `deadline reached`, or `operator error`.
Calculate the Scenario Run Outcome and aggregate Qualification under
[Qualification](../user/QUALIFICATION.md). Incomplete mandatory cleanup prevents a
pass but does not conceal trustworthy product failures.

The run is complete when every selected Check has an outcome, mandatory cleanup has
evidence, owned resources are reconciled, and `summary.md` names the Scenario Run
Outcome, Qualification where applicable, and execution status.

Follow the applicable Scenario assets for the [Taxi submodule companion](../scenarios/taxi/fixtures/submodules.md)
and the D-01/D-02 Booley Feedback probes, which may invoke disposable file-only
fixtures. Those assets own their fixture, evidence, authority, and credit boundaries.
