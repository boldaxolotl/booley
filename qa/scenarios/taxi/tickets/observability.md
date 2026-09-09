### Ticket Create invocations

The scenario is the source of both complete Ticket contracts. Ticket Create must not infer omitted semantics:

- Codex form: `$booley-ticket-create --agent --no-confirm <complete structured scenario payload>`
- Claude-compatible form: `/booley-ticket-create --agent --no-confirm <complete structured scenario payload>`

Both Tickets use:

```yaml
on_success:
  destination: done
  merge: true
  cleanup: true
  triage_report: true
priority: medium
```

Only one automatic retry is permitted, with `max_attempts: 1`, and only for the exact recognized error `API Error: Response stalled mid-stream`. Design failures, mutation survivors, timeouts, crashes, context exhaustion, and usage-limit failures are not automatically retried.

### Ticket 1 — `strengthen-10g-mac-observability`

Title: **Strengthen 10G MAC observability**. Type: `verification`.

Scope is limited to new scenario-owned files:

```yaml
- qa/taxi_eth_mac_10g/test_observability.py [new]
- qa/taxi_eth_mac_10g/test_observability.sv [new]
```

Ticket creation authors this Target Plan entry and its owned test registrations:

```yaml
target_plan:
  - target: sim_mac_10g_observability
    role: persistent
```

The persistent Target reuses the Setup-approved 64-bit, gearbox-disabled, DIC/PTP/PFC/statistics source and parameter contract, uses the new thin wrapper as toplevel and new Cocotb module as its testbench, and remains selectable after acceptance.

The Ticket must implement these deterministic tests:

- **Bad RX FCS and statistics.** Corrupt a received XGMII frame's FCS without changing its payload stimulus; require RX error `tuser`, the discrete bad-FCS indication, and counter record ID 34.
- **Exact PFC class and quanta.** Use an enable vector covering all eight classes and distinct quanta `[10, 20, 30, 40, 50, 60, 70, 80]`; verify the exact transmitted and received class bitmap and each quanta value, not merely the number of MAC control frames. Require the relevant PFC counter records, including IDs 25 and 57.
- **Underrun statistics.** Reproduce the deterministic four-cycle source pause and require the XGMII error termination plus counter record ID 3.
- **TX completion identity.** Send uniquely tagged frames and verify every completion returns the correct 16-bit tag and the timestamp relationship to the observed SFD.
- **Statistics stream typing.** Correctly distinguish `tuser=0` counter records from `tuser=1` string records and verify the expected ID namespace; do not count strings as counters.

Mandatory Criteria are successful Elaboration Check and complete Simulation for `sim_mac_10g_observability`, a clean TB-quality review bound to that Target, and this fixed mutation campaign:

```yaml
mutation_score:
  - target: sim_mac_10g_observability
    scope: [src/eth/rtl/taxi_eth_mac_10g.sv]
    total: 8
    min_detected: 7
```

Mutation proposal steering targets PFC request/ack routing, RX-error propagation, statistics enablement/output, and TX completion timestamp/tag wiring. Specialist Source Isolation must hide the new Cocotb and wrapper sources from the Mutation Tester while it proposes mutations. Preserve the proposal lock, pristine baseline, every isolated mutant result, source variant, first-killing-test evidence, restoration proof, and atomic manifest. One survivor may be reported; fewer than seven detected mutants fails the mandatory Criterion.

The Ticket may not edit any pre-existing Taxi file or weaken an upstream test. Its accepted result is the new `qa/taxi_eth_mac_10g/` verification surface and persistent Target.
