# Goalset: close-test-gap

Add decoder choice 2 to the gap test while retaining reset, counter wrap,
choices 0/1 and assertions. Change only `tb/coverage_tb.sv`; preserve RTL, core,
registry and policy. The dedicated registry has only gap on sim_generated,
so `tests: all` means exactly [gap]. Collect via MCP before/after, ask the Analyst
about the exact initial Campaign, and retain initial passing sim/failing coverage,
advice and final full Campaign. No waivers or threshold reduction. Finish with a
clean committed TB-only diff and Session Summary.

## Goals

```json
[
  {
    "family": "sim",
    "target": "sim_generated",
    "origin": "close-test-gap"
  },
  {
    "family": "coverage",
    "target": "sim_generated",
    "tests": "all",
    "metrics": {
      "cover_property": 100
    },
    "origin": "close-test-gap"
  }
]
```
