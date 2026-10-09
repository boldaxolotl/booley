# Goalset: repair

Repair PFC priority routing from the supplied failing assertion, class behavior,
upstream-test contrast and evidence pointers. Reproduce freshly before editing;
diagnose from visible evidence and ordinary Project inspection. Restore
`src/eth/rtl/taxi_eth_mac_10g.sv` byte-identical to pinned upstream. Never change
tests, Project configuration, Target contracts, verification assets or unrelated RTL.
Keep the five-clock SDC and identical library/recipe for physical baseline/candidate
comparison. Specification review uses the ordinary Project's `docs/taxi-contract.md` approved design
contract copied before entry. Keep complete upstream and observability suites passing;
never hide the failure, waive checks or push.

## Goals

All Goals are mandatory. Copy this file into the resolved Project's `goalsets/`
before entry. Follow the candidate's `booley-goal/SKILL.md` and USAGE Goal Mode.

```json
[
  {
    "family": "elab",
    "target": "sim_mac_10g",
    "origin": "repair"
  },
  {
    "family": "elab",
    "target": "sim_mac_10g_observability",
    "origin": "repair"
  },
  {
    "family": "sim",
    "target": "sim_mac_10g",
    "origin": "repair"
  },
  {
    "family": "sim",
    "target": "sim_mac_10g_observability",
    "origin": "repair"
  },
  {
    "family": "lint",
    "target": "lint_mac_10g",
    "origin": "repair"
  },
  {
    "family": "synth",
    "target": "synth_mac_10g",
    "thresholds": {
      "cell_count_increase_at_most": "0%"
    },
    "origin": "repair"
  },
  {
    "family": "synth",
    "target": "synth_mac_10g_physical",
    "thresholds": {
      "cell_count_increase_at_most": "0%",
      "critical_path_ps_increase_at_most": "0%"
    },
    "origin": "repair"
  },
  {
    "family": "review",
    "review": "rtl_bugs",
    "verdict": "clean",
    "origin": "repair"
  },
  {
    "family": "review",
    "review": "rtl_protocol",
    "verdict": "done",
    "origin": "repair"
  },
  {
    "family": "review",
    "review": "rtl_spec",
    "verdict": "done",
    "spec": "docs/taxi-contract.md",
    "origin": "repair"
  }
]
```
