# Goalset: observability

Strengthen 10G MAC observability using new files only:
`qa/taxi_eth_mac_10g/test_observability.py` and `.sv`, plus ordinary Project
Target/test definitions for `sim_mac_10g_observability`. Reuse Setup's approved
64-bit, gearbox-disabled DIC/PTP/PFC/statistics contract, with the new thin
wrapper top and Cocotb module. Keep the Target selectable after integration.

Implement five deterministic registered tests:
- Bad RX FCS: corrupted FCS with unchanged payload; require RX error `tuser`,
  discrete bad-FCS indication and counter ID 34.
- Exact PFC: all 8 classes, quanta [10,20,30,40,50,60,70,80], exact TX/RX
  bitmap and every quanta value, counters including IDs 25 and 57.
- Four-cycle underrun: XGMII error termination and counter ID 3.
- TX completion: unique 16-bit tags and timestamp relationship to observed SFD.
- Statistics: distinguish `tuser=0` counters from `tuser=1` string records and
  verify the ID namespace; never count strings as counters.

Mutation proposals cover PFC req/ack, RX error propagation, statistics enable/output
and TX timestamp/tag wiring. Hide new Cocotb/wrapper sources from the Mutation
Tester. Retain lock, pristine baseline, isolated variants, first killing tests,
restoration proof and atomic manifest. One survivor is allowed; seven kills are
mandatory. Do not edit existing Taxi files or weaken upstream tests.

## Goals

All Goals are mandatory. Copy this file into the resolved Project's `goalsets/`
before entry. Follow the candidate's `booley-goal/SKILL.md` and USAGE Goal Mode.

```json
[
  {
    "family": "elab",
    "target": "sim_mac_10g_observability",
    "origin": "observability"
  },
  {
    "family": "sim",
    "target": "sim_mac_10g_observability",
    "origin": "observability"
  },
  {
    "family": "review",
    "review": "tb_quality",
    "verdict": "clean",
    "origin": "observability"
  },
  {
    "family": "mutation",
    "target": "sim_mac_10g_observability",
    "scope": [
      "src/eth/rtl/taxi_eth_mac_10g.sv"
    ],
    "min_detected": 7,
    "total": 8,
    "origin": "observability"
  }
]
```
