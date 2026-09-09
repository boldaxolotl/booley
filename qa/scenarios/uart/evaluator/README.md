# Independent UART evaluator

This directory stays in operator-controlled storage outside the Developer's
Project, Session Runtime, mounted folders and network reach. Copy only `../spec/`
and the approved prompts/Ticket payloads into the Project. Never expose this
implementation, generated seed/cases, controls, build logs or raw observations to
the implementing Developer. Source isolation must be demonstrated by the run's
actual filesystem/mount/network evidence; path separation alone does not prove it.

The evaluator reads only the frozen public corpus and addenda. Its simulator
adapter compiles an owned snapshot of explicit, hash-verified RTL from the accepted
Git commit. Literal includes must resolve to explicit hashed `include_files`;
absolute, escaping, ambiguous and macro includes are rejected. Include directives
are rewritten only to the corresponding operator snapshot paths. It does not compile the candidate testbench or trust its pass sentinel.
Provision cocotb 2.1.0 and Icarus in the isolated operator environment before
the run; record their exact versions and immutable image identity in run evidence.

From this directory:

```sh
python cases.py --seed 0123456789abcdef0123456789abcdef --output /operator/run/manifest.json
python controls.py /operator/run/controls
python run.py --candidate /operator/run/candidate-inputs.json \
  --manifest /operator/run/manifest.json \
  --controls /operator/run/controls/controls.json \
  --output /operator/run/evaluation-0
```

The shown seed is illustrative. Supply the run's own 128-bit seed and freeze it
before candidate evaluation. `candidate-inputs.json` contains `root`, the exact
accepted `commit`, an explicit `sources` list of `{path, sha256}` records, and
optional `include_files` records for headers. Paths are relative to the root.
The root must be disjoint from this evaluator. Copy the entire original manifest
unchanged for both repair evaluations. Modified/missing cases, changed evaluator
or public-input digests, and controls for another evaluator are rejected.

Each case resets the device and emits separate stimulus, observations, external-pin
VCD and execution log files. Operational compile/case limits are 300/30 seconds;
the source-clock ceiling is 1,048,576. Budget ceilings do not demonstrate feasibility.
Controls record a shared wall-clock deadline; the initial evaluation uses only its
remaining time. The coordinator further clips controls plus evaluation to the shared 30-minute phase,
repairs plus full reruns to 45 minutes each, and all work to the cleanup boundary.
An operational timeout or evaluator failure is blocked, never an RTL mismatch.

The 43 controls each require positive, independently corrupted and restored
hardware: 129 simulator runs in total. The transport fixture covers MMIO and TX;
the separate receiver fixture covers exact-a RX at all four start phases with
NF=0/1, even/odd parity reception, RX FIFO depth/order, RX watermark state, natural
RX and injected event IRQ observation, transition-rich VAL history, and stalled
exactly-once reads. The broader peripheral fixture sweeps exact-b and fractional
RX, both parity-error modes, RX overflow, every break/parity threshold, every TX
and RX watermark encoding, all three level IRQ sources, every materialized invalid
address for reads and writes, both loopbacks, all override modes, and all reset
contexts. Candidate admission requires every control for this evaluator identity.
Timeout controls separately omit read/receive/event resets, incorrectly reset on
full-FIFO drops or W1C, suppress the IRQ, and move one clock outside each inclusive
30B/34B boundary. A natural timeout IRQ control uses the same public timing bound.
Keep failed control attempts alongside successful corrections.

These fixtures qualify the named observation paths, not complete UART behavior.
They demonstrate that each currently materialized evaluator operation detects a
targeted independent circuit defect and passes again after restoration. They do
not establish candidate conformance, exhaust undocumented interactions, or replace
the full case manifest and actual scenario evaluation.

Depth/event-reset timeout cases retain paired stimuli, per-clock IRQ edges and
FIFO-depth brackets. They use the approved exact-a/VAL=32 public 30–34 bit-time
window; missing or out-of-window events fail, while operational errors stay blocked.
Natural interrupt sources and both active/inactive level injection are exercised.
Invalid-address controls retain occupied FIFO data and every stable CSR across
each rejected operation.

See [CONTRACT.md](CONTRACT.md) for the public oracle derivation.
