# Step 5 — Parity check (optional)

> Part of the `booley-setup` skill. **Optional and non-blocking.** Runs only
> when the plan's decision row 18 selects a parity oracle; invoke alone with
> `booley-setup 5` or `booley-setup parity`. It runs *after* the Step 4 gate is
> green — it never gates setup and never changes `status`.

## What this step is for

The Step 4 gate proves Booley **runs** the design and scores its testbench. It
does not prove Booley reproduces the **native build system's** results — the
Makefile/`*.f`/TCL flow the repo already had. For most ports that gap is closed
for free: Booley reuses the repo's own **self-checking testbench** (the TB that
prints its own pass/fail line), so a passing verdict *is* the golden reference —
the design's own assertions, independent of which EDA tool drove them.

This optional step exists for the cases where that inherited oracle is too weak
and the native flow is available to check against: a **directed** TB with a
bolted-on sentinel rather than real self-checks, or a design where you want to
catch **simulator-semantic drift** (X-propagation, 4-state, race ordering) that
a bare pass/fail line sails past.

## When it applies — the EDA-tool-identity gate

Parity is only meaningful **per phase where Booley's selected EDA tool is the same
as the native flow's EDA tool.** If the native flow simulates with an unsupported commercial simulator and this
project was configured for Verilator, the two disagree on exactly the semantics
a parity check would be hunting for — the comparison folds the signal into the
noise floor. That case is **not comparable**: record it as `none` and move on.

This also dissolves an apparent paradox. "The native build doesn't run here" is
not a normal graceful-skip case sitting next to "Booley runs" — for the *same
design and TB*, they largely stand or fall together. When the EDA tools **match**,
the native flow's availability follows for free: the EDA tool is already present,
because Booley uses it. The only residual reason to skip a matched phase is a
**broken native orchestration** (a Makefile that won't drive in this
environment) — which just means "no golden captured," never a setup block.

So, per phase, run parity **iff**:

1. Booley's EDA tool for the phase == the native flow's EDA tool for the phase, **and**
2. the native flow can actually be driven once to emit a reference.

Otherwise skip that phase and say why in the report.

## First cut — `sim`, Tier 1 (verdict + telemetry)

The first cut covers **simulation only**, at the cheapest useful strength:

1. **Capture once.** Drive the native sim one time (same simulator, so it is
   present) and freeze what it prints: the pass/fail verdict **plus** cheap
   deterministic invariants the TB already emits — final cycle count,
   instruction-retire count, a final register/memory dump if the TB prints one.
   This is a **capture-once golden fixture**, not a dual build on every run —
   Booley never re-invokes the native flow after this.
   **Capture `run.log` immediately after the compared run.** The full raw TB
   output lives only in the per-Target `run.log` (in the resolved Edalize
   build dir); `booley flow sim` stdout carries just a truncated summary
   tail. That `run.log` is at a fixed path, so the next run of the same Target
   overwrites it — copy it aside (e.g. into `.booley_project/parity/`) right
   after the run you are comparing, before running anything else against that
   Target.
2. **Quarantine the golden.** Write it under `.booley_project/` (e.g.
   `.booley_project/parity/sim-<target>.golden`) — **never** the RTL repo's
   tracked tree. Golden dumps are IP-derived and can be large; the
   minimal-footprint guardrail and the "never commit a design's VCDs into
   Booley" rule both apply. In stealth mode it rides the inner
   `.booley_project/` repo; in open mode it stays quarantined even though the
   rest of `.booley_project/` is committed.
3. **Diff.** Run Booley `sim` for the Target and compare its stdout capture
   against the golden. **Same simulator + same design ⇒ expect an exact match**
   on the verdict and on deterministic telemetry — there is no tolerance band at
   Tier 1 (tolerance is a synthesis notion). A mismatch is a real **port
   defect**: a wrong fileset, a missing `+define`, wrong TB plusargs, an
   un-built firmware input. Chase the config, not the golden.
   **One known-benign carve-out under Icarus.** Icarus prints
   `$finish called at <time> (<path>)`, and `<path>` is the run's staging
   directory — Booley runs under the Edalize build dir, the native flow under
   its own tree — so that one line differs by construction even on a correct
   port. Normalize or exclude the embedded path before the byte-diff; treat
   *only* the path token as benign. Do not extend that reasoning to any other
   diff — a divergence anywhere else is a real port defect, not "path noise."

## Output — `PARITY-REPORT.md`

Write `.booley_project/PARITY-REPORT.md`. Per Target: the tier used, the golden
fixture path, the diff result (match / mismatch + the diverging lines), and —
explicitly — **what was not compared** (every phase skipped for EDA-tool mismatch or
a missing golden, with the reason). A parity check that quietly narrows its own
scope reads as "everything matched" when it did not. Fold a one-paragraph
summary into Step 4's final report in the **onboarding voice**: a newcomer does
not know what "parity" buys them, so say plainly what was checked against the
old flow, what matched, and what could not be compared.

A mismatch here is a **finding, not a gate**: surface it, but setup already
completed at Step 4. The user decides whether a divergence blocks their use.

## Deliberately out of scope

- **Perpetual dual-build.** The native flow is captured once. Re-run this step
  to refresh the golden.
- **Auto signal-mapping.** Tier 2 (waveform parity — diff a VCD/FST over a
  chosen scope and settle window) needs a *user-authored* signal map, because
  hierarchy paths, timescale, and net names differ between simulators. Guessing
  the mapping produces a green diff that means nothing. Left as a documented
  future extension; not in this cut.
- **Synthesis parity.** A future **metrics band** (capture native area/Fmax
  once, flag Booley outside a **user-set** tolerance — no default band, tech
  makes it project-specific) is a plausible extension. **Formal LEC is out of
  scope entirely** — Booley has no equivalence-checking support to build it on.
