# Execute a Scenario Run

Execute the selected Checks and supporting Steps in Scenario order. The Scenario's
`prepare` phase runs here.

When the selected production Scenario's shared pre-run requirements permit it,
Project Initialization, including `booley init`, is ordinary authorized Scenario
work. It may build or reconcile its declared managed images and toolchains; preserve
their pre-state, command evidence, and resulting identities with the producing Step.

Use documentation and packaged skills matching the tested build, CLI or MCP help,
and ordinary Project inspection. Consult source only to verify or classify behavior
after preserving the original Observation. Append unexpected behavior, incidental
facts, friction, impressions, and wins to `observations.jsonl` without deciding
whether they are Findings.

Record active sub-agent assignments in `operator-state.json` and reconcile them before
finishing execution. Sub-agents cannot change authority or acceptance requirements.
Literal commands or prose are mandatory only when their form is under test.

Preserve evidence identity, freshness, underlying grades, and artifact meaning. Reuse
an artifact only when it independently supports every linked claim; agent prose cannot
replace it. Capture client claims in the supported client. Visual claims also require
timestamped evidence from a qualified observer. Diagnostic text is authoritative only
when the text is the contract.

A failed prerequisite blocks dependent work until the required state is restored.
Independent work may continue while authority, evidence, and resources remain
controlled.

An expected seeded failure passes its negative Check. Preserve unexpected product
failures after workarounds or retries; later success does not turn them into passes.

Record alternatives and deviations with the affected Check. A declared alternative
may satisfy it. An undeclared change to inputs, actions, authority, or evidence blocks
the claim unless trustworthy failure evidence already exists; preserve that failure.
Use an exact `caused_by_result_ids` link only when a prior Check Result caused the
current result. Shared messages, tools, resources, and timing alone are not causality.

Execution ends when every selected Check has a Check Result and no active assignment
or uncertain mutation remains. On a deadline, loss of control, or other stop, record
the reason and outstanding Checks. Set Protocol Stage `finish`, then read
[Finish](FINISH.md).
