# Implementation status

Local implementation of the agreed design at `b163fd1f45b76f3950005678e500e695232832fb`.
No branch push, PR, merge, external report or scenario campaign is included.

Implemented assets include the strict scenario schema and validator, 62 capabilities
plus 16 EDA references, seven explicit profiles, all 1,072 reviewed check IDs,
extracted prompts and Ticket contracts, pinned UART documentation with verified
SHA-256 values, interface/MMIO/timing addenda, five-clock Taxi SDC, and deterministic
local A/B recursive submodule construction. The independent UART materializer and
simulator adapter are present, with transport control tests; they remain under
implementation review and cannot yet establish complete conformance.

Validation performed includes focused validator rejection tests and deterministic
UART oracle/materialization tests. The submodule fixture was simulated using local
image `sha256:43a16cce254f7f5e200d6029703ba9842d15bb053ebf59bb62f2775f17f0d5d2`:
complete B passed; required top-level/nested source absence failed with warm build
products present; cold absence failed; exact restorations passed freshly. Independent
UART controls on the same image passed positive, separately corrupted MMIO/serial,
and restored expectations. These are fixture controls, not public Booley execution
or candidate UART conformance. Original adapter failure logs remain in local operator
evidence at `/tmp/qa-submodule-controls`.

Still incomplete:

- The supplemental probe assets preserve individual stimuli/oracles, but several
  provider-failure, host-policy and legacy VS Code cases still need concrete safe
  public-entry-point injection mechanisms and behavioral control tests.
- Prerequisites and evidence need a final per-check semantic review. Reference
  validation cannot establish that every combined stimulus proves each named outcome.
- UART timeout pairs now retain depth-change, event-reset and full-drop observations,
  but lack a defensible universal comparison tolerance and remain blocked. Natural
  interrupt sources are exercised; RX phase/address side-effect control coverage and
  the additional oracles still need independent behavioral qualification.
- Required GUI/native Windows/provider/Vivado infrastructure and every real profile
  execution remain unqualified. Eight-hour feasibility has not been measured.

Do not check off full implementation or qualification in the reviewed handoff until
these corresponding items are actually complete. Preserve all original profile
selections and quantitative limits while resolving the gaps.

The follow-up review fixed unvalidated RTL includes/live-source races, partial
manifest publication, fixture failure cleanup, source-inventory overwrite, RO
RDATA/VAL write observations, and second-break rearm coverage. Regression tests
also compare every profile selection and platform exclusion against the frozen
reviewed lists, independently of production profile construction.

Final local verification for this checkpoint: 25 focused tests passed; pinned
Ruff 0.16.6 passed across `src/`, `tests/` and `qa/`; whole-suite authoring
validation emitted the reverse coverage index successfully. All seven profile
memberships and exclusions match the reviewed lists. Six real UART control
verdicts passed again against the final evaluator identity, with evidence at
`/tmp/qa-submodule-controls/uart-controls-4`. No full profile was executed.
