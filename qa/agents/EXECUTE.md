# Execute a Scenario Run

Read [Record](RECORD.md). Execute selected Checks in Scenario order. Read each Step's
action, prerequisites, Checks, assets, recovery instructions, authority, and retry
limits together.

You may assign setup, development, evaluation, diagnostics, and quiescence to
sub-agents, but retain responsibility for sequencing, evidence, and the report.
Record each assignment and identity with its Step and in `operator-state.json` while
active. Reconcile assignments before advancing stages. Sub-agents cannot grant
authority or change acceptance requirements. Literal commands or prose are mandatory
only when their form is under test.

Each Check declares its stimulus, expectation, contract source, and evidence. Preserve
artifact identity and freshness. Reuse an artifact only when it independently supports
every linked claim. Agent prose cannot replace artifacts. Runtime Attachment claims
require evidence from the attached application; Waveform Viewer claims require
timestamped visual evidence from a qualified observer.

Discovery evidence includes identity-bound output and meaningful input rejection when
supported. Booley Flow and EDA evidence retains the normalized grade and fresh
artifacts with Target, Booley Flow, and tool identities. Stateful Checks prove their
declared transitions, failure, recovery, persistence, or product cleanup. Interactive
Mode and Ticket Mode Checks correlate Runtime Attachment and backend identities with
durable logs and artifacts. Diagnostic console text is authoritative when it is the
contract. Preserve underlying grades and artifact meanings.

Apply declared timeouts and narrow retry allowances. A failed prerequisite blocks
dependent work until the required state is demonstrably restored; continue independent
work while authority, evidence, and resources remain controlled.

A seeded fault must prove baseline success, inject the declared fault, observe the
expected failure, restore state, and prove recovery. The expected failure passes its
negative Check. Preserve unexpected product failures after workarounds or retries;
post-recovery success does not turn them into passes.

Record alternatives and deviations with the affected Check. A declared alternative
may satisfy it. An undeclared change to inputs, actions, authority, or evidence blocks
the claim unless trustworthy failure evidence already exists; preserve that failure.

Normal execution is complete when every selected Check has an append-only Check
Result and no active assignment or uncertain mutation remains. On a deadline, loss
of control, or other declared stop, record the stop reason and outstanding Checks
instead. In either case, set Protocol Stage `finish`, then read
[Finish](FINISH.md).
