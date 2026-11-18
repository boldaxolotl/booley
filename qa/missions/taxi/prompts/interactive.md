Work read-only in the setup-complete pinned Taxi Project. Confirm the Sandbox identity, repository cleanliness, Doctor state, and available Target identities. Compare CLI and MCP Target discovery and inspect the resolved build inputs through the CLI detail view. Run the focused PFC test on `sim_mac_10g` with a fresh FST trace. Exercise B-Wave on that trace as listed below, then invoke a TB-quality Reviewer against `sim_mac_10g`. Preserve every structured report and artifact. Do not edit files, create commits, weaken checks, or push anything.

On the FST trace:

- Prove a fresh, nonempty, identity-bound FST with scope, signal-count, size, and tick metadata.
- Register a named alias and `_last`, prove the named alias after a new command context, create/list/resolve/delete named markers, and reject a stale or missing registration.
- Use `list`, `signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, and `stuck` semantically against known PFC request, frame-start, XGMII data/control, timestamp, reset, and statistics relationships.
- Cover synchronous and asynchronous views, explicit clock/reset selection, cycle and typed physical-time tokens, and one request-to-frame latency cross-checked against the Cocotb observation rather than trusted from CLI return code alone.
- Open a scoped Waveform Viewer state containing the clock, PFC request, packet-start, XGMII data/control, and relevant statistics signals, with start/end markers and cursor. Save the `bwave gui` readback output. Attempt visual capture only when the host can actually observe the Waveform Viewer (a display and a working screenshot route); otherwise log it as skipped.
