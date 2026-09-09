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
Cocotb and Icarus must be provisioned in the isolated operator environment before
the run. The exercised control environment used cocotb 2.1.0 and the exact local
image recorded in the implementation validation record; this is not a product
release qualification claim.

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

The controls deliberately implement only transport and serial behavior. Positive,
separate MMIO/serial one-bit corruption, and restored runs qualify those observation
paths; they do not establish full UART functionality. Keep the original failed
control attempts alongside successful corrections.

Depth/event-reset timeout cases retain paired stimuli and timestamps but remain
blocked: the public text does not supply a universal phase-comparison tolerance.
Natural interrupt sources and both active/inactive level injection are exercised.
Invalid-address cases now retain occupied FIFO data and every stable CSR across
the rejected operation. Full RX sampling phase coverage and real hardware controls
for these additional oracles still require qualification.
No production UART candidate has been evaluated here.

The active derivation is [CONTRACT.md](CONTRACT.md); historical handoff notes stay
at their published design commit.
