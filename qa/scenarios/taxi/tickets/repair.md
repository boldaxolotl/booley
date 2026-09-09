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

### Ticket 2 — `repair-pfc-priority-routing`

Title: **Repair PFC priority routing**. Type: `bugfix`. Dependency: **Strengthen 10G MAC observability**.

Scope:

```yaml
- src/eth/rtl/taxi_eth_mac_10g.sv
```

Required behavior:

- Reproduce the exact class-rotation failure on a fresh run and trace.
- Diagnose the routing defect using the provided evidence plus ordinary Project inspection; do not rely on the hidden seed description.
- Repair the RTL without changing tests, Criteria, Project configuration, unrelated RTL, or accepted verification assets.
- Preserve the complete upstream regression, exact PFC/quanta behavior, statistics behavior, timestamp/tag behavior, Verible cleanliness, and logical synthesis quality.
- Leave the repaired `taxi_eth_mac_10g.sv` byte-identical to the pinned upstream revision.

Mandatory Criteria:

- Elaboration Check for `sim_mac_10g` and `sim_mac_10g_observability`.
- Complete upstream `sim_mac_10g` remains pass-to-pass.
- `sim_mac_10g_observability` records the seeded fail-to-pass transition.
- `lint_clean` for `lint_mac_10g`.
- `synthesis_ok` for the directed Acceptance Basis/candidate pair of `synth_mac_10g`, with `cell_count_increase_at_most: 0%`.
- `synthesis_ok` for the directed Acceptance Basis/candidate pair of `synth_mac_10g_physical`, with `cell_count_increase_at_most: 0%` and `critical_path_ps_increase_at_most: 0%`. Keep the approved five-clock SDC and identical library/recipe on both sides.
- Clean RTL bugs review, plus terminal RTL protocol and RTL specification reviews.

The Ticket must not weaken or skip tests, alter the Target contract, hide the failure, add waivers, or push a branch.
