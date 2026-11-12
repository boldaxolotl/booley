# Goalset: close-test-gap

Close the coverage gap in the testbench: make the `gap` test also drive decoder
choice 2, while it still checks reset, counter wrap, choices 0 and 1, and every
assertion it has today.

- Change only `tb/coverage_tb.sv`. Leave the RTL, the core file, the test
  registry and the coverage policy as they are. Add no waivers and lower no
  threshold.
- This Project's registry lists only `gap` for `sim_generated`, so
  `"tests": "all"` below means exactly `[gap]`.
- Collect coverage through MCP before and after the change. Ask the Coverage
  Analyst about the exact first Campaign.
- Keep the first collection's evidence (simulation passes, coverage fails),
  the Analyst's advice, and the final full Campaign.
- Finish with a clean, committed diff that touches only the testbench, and a
  Session Summary.

## Goals

```json
[
  {
    "family": "SIM",
    "target": "sim_generated",
    "origin": "close-test-gap"
  },
  {
    "family": "COVERAGE",
    "target": "sim_generated",
    "tests": "all",
    "metrics": {
      "cover_property": 100
    },
    "origin": "close-test-gap"
  }
]
```
