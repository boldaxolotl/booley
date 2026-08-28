# Make Ticket acceptance evidence durable

Booley records each normalized strict-Ticket Criterion outcome as immutable
**Acceptance Evidence** outside the disposable runtime directory. The record is
written at the MCP completion boundary after aliases, thresholds, and structural
guards determine the effective outcome. Its sequence is assigned atomically in
completion order; its execution ID is provenance rather than Ticket candidate
identity, and baseline/candidate roles remain explicit.

The authoritative acceptance boundary fences outstanding Jobs, reuses the
existing Criteria acceptance validator, and freezes a content-addressed
**Acceptance Snapshot** before moving the Ticket to review or done. A durable
accepted reference selects that snapshot, and a prepared review package is
bound to the same digest. Completion rejects corrupt selected evidence. Doctor
self-tests run in diagnostic mode with Ticket context scrubbed, so they cannot
create or overwrite Ticket acceptance evidence.

Running Tickets continue to use `booley_state.json` as a convenient mutable
execution projection. Review and done readers take Criteria from the accepted
snapshot but may overlay live timeline and cost data field by field. Legacy
terminal Tickets without a snapshot report acceptance as unavailable rather
than fabricating evidence or displaying `0/N`; an in-flight legacy Ticket may
freeze a snapshot from its validated live state at handoff.

## Considered options

- Keeping `booley_state.json` authoritative was rejected because runtime cleanup
  and unrelated diagnostic runs can remove or mutate it.
- Keying acceptance by execution ID was rejected because resuming a Ticket
  creates a new execution generation without creating a different candidate.
- Copying raw MCP input was rejected because aliases, thresholds, freshness,
  and structural guards can change the effective Criterion outcome.
