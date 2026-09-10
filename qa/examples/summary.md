# Illustrative report — no execution claim

Run: `example-run-20260908T120000Z`; Profile run definition: `example-run`; scenario:
`example-wishbone`; inputs: [run.json](run.json).

| Scope | Verdict | Evidence |
|---|---|---|
| Example core slice | passed | `r1`, `r2`, `r3`, `r5` |
| Example GUI slice | incomplete | `r4`: pre-run observer unavailable; cleanup `r5` passed |
| Full example qualification | incomplete | Required GUI check unavailable |

Operational completion: completed. Cleanup: complete. No Findings in this
illustrative slice. Check outcomes: four passed, one unavailable. Records:
[results.jsonl](results.jsonl). Evidence references are fictional examples.

No complete journey, VS Code Runtime Attachment, Waveform Viewer, or real Booley
release is qualified by this example.
