# Illustrative report — no execution claim

Scenario Run: `EXAMPLE_EXECUTION_ID`; Configured Scenario: `example-run-vscode`; Scenario: `example-wishbone`; inputs: [run.json](run.json).

Scenario Run Outcome: incomplete — the selected Viewer Check is unavailable.
Qualification: incomplete — this required run is incomplete, and no run against the
required CLI Configured Scenario is included.

| Component | Status | Evidence |
|---|---|---|
| Example core slice | passed | `r1`, `r2`, `r3`, `r5` |
| Example GUI slice | incomplete | `r4`: pre-run observer unavailable; cleanup `r5` passed |

Execution status: completed. Cleanup: complete. No Findings in this
illustrative slice. Check outcomes: four passed, one unavailable. Records:
[results.jsonl](results.jsonl). Evidence references are fictional examples.

No complete Scenario, VS Code Runtime Attachment, Waveform Viewer, or real Booley
product revision is qualified by this example.
