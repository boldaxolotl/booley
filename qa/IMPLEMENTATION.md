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
- UART paired timeout reset/nonreset stimuli, natural interrupt-source coverage and
  some RX phase/address side-effect controls remain incomplete. The evaluator reports
  its unimplemented timeout comparisons blocked rather than fabricating a pass.
- Required GUI/native Windows/provider/Vivado infrastructure and every real profile
  execution remain unqualified. Eight-hour feasibility has not been measured.

Do not check off full implementation or qualification in the reviewed handoff until
these corresponding items are actually complete. Preserve all original profile
selections and quantitative limits while resolving the gaps.
