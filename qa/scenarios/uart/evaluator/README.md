# Independent UART evaluator

This directory stays in operator-controlled storage outside the Developer's
Project, Session Runtime, mounted folders and network reach. Copy only `../spec/`
and the approved prompts/Ticket payloads into the Project. Never expose this
implementation, generated seed/cases, controls, build logs or raw observations to
the implementing Developer. Source isolation must be demonstrated by the run's
actual filesystem/mount/network evidence; path separation alone does not prove it.

The evaluator reads only the frozen public corpus and addenda. Its simulator
adapter compiles an explicit, hash-verified list of RTL files from the accepted
Git commit. It does not compile the candidate testbench or trust its pass sentinel.
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
accepted `commit`, and an explicit `sources` list of `{path, sha256}` records.
The root must be disjoint from this evaluator. Copy the entire original manifest
unchanged for both repair evaluations. Modified/missing cases, changed evaluator
or public-input digests, and controls for another evaluator are rejected.

Each case resets the device and emits separate stimulus, observations, external-pin
VCD and execution log files. Operational compile/case limits are 300/30 seconds;
the source-clock ceiling is 1,048,576. Budget ceilings do not demonstrate feasibility.
The coordinator clips controls plus evaluation to the shared 30-minute phase,
repairs plus full reruns to 45 minutes each, and all work to the cleanup boundary.
An operational timeout or evaluator failure is blocked, never an RTL mismatch.

The controls deliberately implement only transport and serial behavior. Positive,
separate MMIO/serial one-bit corruption, and restored runs qualify those observation
paths; they do not establish full UART functionality. Keep the original failed
control attempts alongside successful corrections.

The depth/event-reset timeout comparisons still need paired stimulus implementation
and review. Those cases currently return blocked, so this evaluator cannot yet
establish complete conformance. Natural interrupt-source identity, full RX sampling
phase coverage, and complete invalid-address side-effect observations also require
further control coverage. No production UART candidate has been evaluated here.
