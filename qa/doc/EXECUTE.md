# Execute a Scenario Run

Before each Step, render the frozen run's selected-execution projection and navigate
that projection rather than the raw Scenario order:

```sh
python3 qa/selected_execution.py <run-root> --suite-root <frozen-suite-root>
```

Execute only the selected Check capture points and their displayed supporting Steps,
in projection order. A supporting Step remains required even when its own Checks are
unselected; do not capture those unselected Checks. The Scenario's `prepare` phase
runs here.

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

Before every mutating command, apply the mutation gate in this order: atomically
publish the intended command and relevant pre-state in `operator-state.json`; publish
the complete cleanup-ledger candidate through `qa/record_cleanup.py`, including a
planned identity for every resource that may be acquired; only then execute the
command. Immediately after acquisition, replace each planned identity with the exact
identity and publish the post-state before any dependent command.

Preserve evidence identity, freshness, underlying grades, and artifact meaning. Reuse
an artifact only when it independently supports every linked claim; agent prose cannot
replace it. Capture client claims in the supported client. Visual claims also require
timestamped evidence from a qualified observer. Diagnostic text is authoritative only
when the text is the contract.

Before appending any Check Result, finish every referenced file beneath the Scenario
Run's `evidence/` directory. When the source is external, copy its bytes to a
deterministic run-owned path, record source and destination digests (or equivalent
byte-identity proof), and make the destination immutable under the Scenario's evidence
procedure before invoking `record_check.py`. This applies to top-level, deviation,
borrowed-preservation, and recovery evidence. The recorder rejects external paths and
never performs an implicit copy.

A failed prerequisite blocks dependent work until the required state is restored.
Independent work may continue while authority, evidence, and resources remain
controlled.

An expected seeded failure passes its negative Check. Preserve unexpected product
failures after workarounds or retries; later success does not turn them into passes.

Record alternatives and deviations with the affected Check. A declared alternative
may satisfy it. An undeclared change to inputs, actions, authority, or evidence blocks
the claim unless trustworthy failure evidence already exists; preserve that failure.
Use an exact `caused_by_result_ids` link only when a prior Check Result caused the
current result and its Check's Step directly requires the prior Check's Step. Retain
evidence explaining why that result prevented this Check's stimulus. Shared messages,
tools, resources, and timing alone are not causality. If declared prerequisites pass,
run the independent stimulus rather than marking it blocked by another Step's failure.

Execution ends when every selected Check has a Check Result and no active assignment
or uncertain mutation remains. On a deadline, loss of control, or other stop, record
the reason and outstanding Checks. Set Protocol Stage `finish`, then read
[Finish](FINISH.md).
