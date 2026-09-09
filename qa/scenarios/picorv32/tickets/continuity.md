### Ticket Create invocations

The scenario itself is the source of both Tickets. Do not ask Ticket Create to infer missing semantics. Pass the complete field sets below in one creation phase:

- Codex form: `$booley-ticket-create --agent --no-confirm <complete structured scenario payload>`
- Claude form: `/booley-ticket-create --agent --no-confirm <complete structured scenario payload>`

These are skill invocations, not ordinary CLI commands. Enqueue automatically publishes the immutable Acceptance Basis; there is no manual seal, Target Contract, `base_sha`, or second confirmation. Ticket creation may author only the approved Target definitions, owned `tests.toml` tables, and empty `[new]` placeholders. The Developer Agent authors the implementation.

Both Tickets use:

```yaml
on_success:
  destination: done
  merge: true
  cleanup: true
  triage_report: true
priority: medium
```

Only one automatic retry is permitted, with `max_attempts: 1`, and only when the exact recognized error is `API Error: Response stalled mid-stream`. Ordinary crashes, test failures, timeouts, context exhaustion, and usage-limit failures are not retried.

### Ticket 1 — `dhrystone-self-checking-cycle-contract`

Type: `verification`.

Scope:

```yaml
- dhrystone/dhry_1.c
- dhrystone/testbench.v
```

Required implementation:

- Keep the fixed 100-iteration demo.
- Validate the deterministic final Dhrystone result in firmware. A mismatch prints an error and traps before success or cycle reporting.
- Preserve success magic `123456789` to MMIO address `0x20000000`; the testbench recognizes it and only the validated success path may pass.
- Emit exactly `[SIM_CYCLES] dhry <User_Time>` after validation, with a deterministic timeout.
- The pinned calibration uses xPack GCC 15.2 and has `User_Time = 109734` cycles. Acceptance uses the absolute inclusive cap `110000`; it does not require a baseline cycle count.

Ticket creation authors this Target Plan entry and its owned test registration:

```yaml
target_plan:
  - target: sim_dhry_checked
    role: persistent
```

Register `[sim_dhry_checked] dhry`. Criteria are mandatory:

```yaml
elab_pass: [sim_dhry_checked]
sim_pass:
  - dhrystone/testbench.v @ sim_dhry_checked @ dhry @ pass -> pass
cycle_count:
  - target: sim_dhry_checked
    test: dhry
    cycle_count_max: 110000
review_tb_quality_done:
  target: sim_dhry_checked
```

The persistent Target remains selectable after acceptance and is the provider exported to Ticket 2.
