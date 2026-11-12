# Independent UART evaluator

How the QA operator runs the uart mission's independent evaluator in the
`evaluate` and `goal-repair` areas. [CONTRACT.md](CONTRACT.md) derives the
public oracles and case IDs; this file covers isolation, inputs, commands and
limits.

## Isolation

The evaluator grades the Goal child's RTL, so the child must never see how it
grades. Keep this directory in operator-controlled storage outside the child's
Project, Sandbox, mounted folders and network reach. Copy only `../spec/` and
the approved prompts and Goalsets into the Project. Never expose this
implementation, the generated seed or cases, controls, build logs or raw
observations to the child. Demonstrate isolation from the run's actual
filesystems, mounts and network routes; path separation alone does not prove
it.

The evaluator reads only the frozen public corpus and addenda. Its simulator
adapter compiles an owned snapshot of explicit, hash-verified RTL from the
accepted Git commit. Literal includes must resolve to explicit hashed
`include_files`; absolute, escaping, ambiguous and macro includes are rejected,
and include directives are rewritten only to the matching operator snapshot
paths. It never compiles the candidate testbench or trusts its pass sentinel.
Provision cocotb 2.1.0 and Icarus in the isolated operator environment before
the run; note their exact versions and the immutable Sandbox Image identity in
`log.md`.

## Run it

From this directory:

```sh
python cases.py --seed 0123456789abcdef0123456789abcdef --output /operator/run/manifest.json
python controls.py /operator/run/controls
python run.py --candidate /operator/run/candidate-inputs.json \
  --manifest /operator/run/manifest.json \
  --controls /operator/run/controls/controls.json \
  --output /operator/run/evaluation-0
```

The seed shown is illustrative. Supply the run's own 128-bit seed (32
lowercase hex digits) and freeze it before candidate evaluation. For each
repair rerun, pass the entire original manifest unchanged, a new `--output`
(`evaluation-1`, `evaluation-2`) and `--phase-seconds 2700`.

`candidate-inputs.json` contains `root`, the exact accepted `commit`, an
explicit `sources` list of `{path, sha256}` records, and optional
`include_files` records for headers. Paths are relative to the root. `root`
must be a Git checkout whose `HEAD` is exactly `commit`, such as the Goal
worktree or a detached clone at that commit, and must be disjoint from this
evaluator. The evaluator reads each source's bytes from that commit through
Git and rejects a working-tree file that differs from it. It also rejects
modified or missing cases, changed evaluator or public-input digests, and
controls recorded for another evaluator.

## Limits and outcomes

Each case resets the device and emits separate stimulus, observation,
external-pin VCD and execution log files. Operational limits are 300 seconds
per compile and 30 seconds per case; the source-clock ceiling is 1,048,576.
These ceilings bound resources; they do not show that a case is feasible.

Controls record a shared wall-clock deadline, and the initial evaluation uses
only what remains of it: controls plus evaluation share one 30-minute phase.
Each repair plus its full rerun gets 45 minutes. The operator stops starting
new work when the mission timebox runs out. An operational timeout or
evaluator failure is a blocked result, never an RTL mismatch.

## Controls

Controls prove the evaluator can see defects before it grades a candidate.
Each of the 45 controls runs positive, independently corrupted and restored
hardware: 135 simulator runs in total. Candidate admission requires every
control for this evaluator identity. Keep failed control attempts alongside
successful corrections under `evidence/`.

- The transport fixture covers MMIO and TX.
- The receiver fixture covers exact-a RX at all four start phases with NF=0/1,
  even/odd parity reception, RX FIFO depth and order, RX watermark state,
  natural RX and injected event IRQ observation, transition-rich VAL history,
  and stalled exactly-once reads.
- The peripheral fixture sweeps exact-b and fractional RX, both parity-error
  modes, RX overflow, every break/parity threshold, every TX and RX watermark
  encoding, all three level IRQ sources, every materialized invalid address for
  reads and writes, both loopbacks, NCO=0 TX stall, all override modes
  (including override with TX enabled), and all reset contexts. Invalid-address
  controls preserve occupied FIFO data and every stable CSR across each
  rejected operation.
- Timeout controls separately omit read/receive/event resets, wrongly reset on
  full-FIFO drops or W1C, suppress the IRQ, and move one clock outside each
  inclusive 30B/34B boundary. A natural timeout IRQ control uses the same
  public timing bound.

Controls show that each currently materialized evaluator operation detects a
targeted, independent circuit defect and passes again after restoration. They
do not establish candidate conformance, exhaust undocumented interactions, or
replace the full case manifest and the actual candidate evaluation.

## Timeout cases

Depth- and event-reset timeout cases keep paired stimuli, per-clock IRQ edges
and FIFO-depth brackets. They use the approved exact-a/VAL=32 public 30–34
bit-time window from `../spec/timing-addendum.md`: missing or out-of-window
events fail, while operational errors stay blocked. Natural interrupt sources
and both active and inactive level injection are exercised.
