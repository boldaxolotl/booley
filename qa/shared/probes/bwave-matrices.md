# B-Wave command matrices

[Reconcile the B-Wave virtual-signal option matrix](https://github.com/boldaxolotl/booley/issues/282)
defines the virtual-signal contract. Under
`taxi-10g-mac-port-evolution.inventory.W-04`, create these **per-command** checks:

| Command set | Check suffixes (one ID for every named command) | Stimulus / expected result / evidence |
|---|---|---|
| wave, find, sample, distance, value | `virtual-<command>-semantic` | Pass a composite virtual definition and consume it as an observable row, target, trigger, snapshot or event appropriate to that command. Require exact ground-truth value/time, not accepted argv. Retain definition, event-table oracle, output. |
| wave, find, sample, distance, value | `virtual-<command>-malformed` | Supply malformed definition → exit 2 with definition diagnostic. Retain argv/exit/stderr and demonstrate corrected definition in the semantic check. |
| list, signal, diff, stats, stuck | `virtual-<command>-rejected` | Supply --virtual → argument-parser rejection, exit 2. Retain live help/public-page and diagnostic; silent ignore is failure. |
| build (outside ten-command query matrix) | `virtual-build-rejected` | Supply --virtual → argument-parser exit 2. Meta gui/schema/docs/skill have no virtual-query surface and gain no virtual support claims. |

Judge the released SUT against these semantics; parser acceptance alone is insufficient.
Only find among behavior-backed virtual commands has schema-backed JSON output.

Under `taxi-10g-mac-port-evolution.inventory.W-03`, independently define `json-<command>-supported`
for list/value/find/stats: request JSON on known trace → parse documented envelope
and exact result. Define `json-<command>-rejected` for signal/wave/sample/diff/
distance/stuck: request JSON → documented unsupported-output rejection. Merely
having --format in help does not establish JSON support. Capture each output,
exit and release-matched command page.

Current [public marker reference](../../../crates/bwave/docs/public/reference/markers.md)
and [overview](../../../crates/bwave/docs/public/commands/overview.md) agree:
**native --marker is wave-only**.
Under `taxi-10g-mac-port-evolution.inventory.W-04`, encode separate `marker-wave-render` (known typed
in-window time → label at correct column), `marker-wave-async-column` (no signal
transition at annotation tick → marker column nevertheless present),
`marker-wave-outside` (out-of-window marker → omitted), `marker-wave-repeat`
(repeated name → last occurrence wins), `marker-wave-shared-time` (two names at
same tick → comma-separated label), `marker-wave-negative` (negative time →
argument-parser rejection), and `marker-wave-async-bare` (bare async time →
rejection). Retain each output and independent time oracle. Define separate
`marker-<command>-rejected` checks for build, list, signal, value, find, sample,
diff, distance, stats, stuck, schema, docs, and skill: native --marker must fail
argument parsing with exit 2. Wrapper GUI gains no native marker-query claim.

Under `taxi-10g-mac-port-evolution.inventory.W-02`, `marker-wrapper-value` supplies a persisted marker
name to value --at → known cycle value; `marker-wrapper-diff` supplies two names
to diff → exact endpoints/delta; `marker-wrapper-wave` supplies named endpoints
to wave → expected interval and annotations. Retain registry snapshots and
outputs separately. Wrapper time-name substitution does not imply native option
support on value/diff.
