# Session Runtime image contract

This document is the compatibility boundary for Booley's standard and RISC-V
Session Runtime images. Image-size work may change packaging, layers, debug
symbols, and implementation details, but it must not remove the behavior below
without a separately reviewed contract change.

The executable version of this contract is
[`session-runtime.toml`](../../.github/contracts/session-runtime.toml). CI runs
[`image_contract.py`](../../.github/scripts/image_contract.py) against built
images and retains JSON evidence. Focused image integration tests remain the
authority for behavior that cannot be represented by command and file probes.

## Common runtime contract

The image runs as the unprivileged `agent` user (UID/GID 1000), starts in
`/work`, and supports writable project bind mounts owned by that user. Booley
starts Flow subprocesses without network access. Agent traffic is constrained
by the Session Runtime's egress proxy and by system policy which disables
provider-hosted web tools.

The following command families are required:

- Shell and project compilation: Bash, POSIX `sh`, Git, Make, GCC, and G++.
- Python: Python 3.13, pip, Booley, `booley-mcp`, FuseSoC/Edalize, cocotb, and
  the curated Python dependencies installed by the image recipe.
- Simulation and waveform work: Icarus (`iverilog`, `iverilog-vpi`, `vvp`),
  Verilator's normal, debug, and coverage commands, cocotb's simulator
  libraries, and B-Wave.
- Synthesis and lint: Yosys with ABC and slang, sv2v, OpenROAD, and the complete
  shipped Verible command suite. The complete Verible suite remains contractual
  until a later contract review narrows it.
- Agent clients: Node.js, npm, Claude Code, and Codex in their publisher-provided
  launch form.

Native C and C++ source must compile, link, and execute in the image. Verilator
generated C++ must compile with its installed runtime headers. Icarus must keep
its VPI development header, targets, modules, and cocotb VPI library. OpenROAD
must keep its OR-Tools shared-library runtime and must perform physical flows;
falling back to logical Yosys does not satisfy this contract.

Claude and Codex must retain version and help diagnostics, offline startup,
their managed web-isolation policy, normal exit status, and signal propagation.
The Claude Python SDK must resolve the system Claude executable; its duplicate
bundled executable is intentionally absent. The existing production-image
agent-policy probe validates publisher package integrity and policy behavior.

Yosys, ABC, and sv2v are shipped stripped. The two installed B-Wave paths are
one hard-linked inode. These properties are image-level assertions, not merely
Dockerfile text checks.

## Rust is not included

The standard Session Runtime does **not** provide Cargo, `rustc`, `rustup`, or a
Rust standard library. B-Wave is compiled in a throwaway builder stage and only
its runtime binary is copied into the final image. A project which compiles Rust
must use a reviewed project image that adds the required pinned toolchain.

This exclusion is deliberate: the Rust toolchain is large and cannot rebuild
B-Wave from the installed wheel because the crate source is not shipped there.
Setup guidance must not advertise Rust as a standard-runtime capability.

## RISC-V extension contract

The RISC-V image inherits the standard image and adds, without replacing its
RootFS history:

- the xPack bare-metal compiler under the `riscv-none-elf-` prefix plus complete
  `riscv32-unknown-elf-` and `riscv64-unknown-elf-` helper aliases;
- all 32 publisher multilib entries, including RV32 and RV64 C and C++ support;
- Spike's four installed programs and its installed libraries;
- `srec_cat`, device-tree compiler, and `pdftotext`; and
- the pinned offline ISA, debug, and psABI reference set under
  `$BOOLEY_RISCV_DOCS`.

CI compiles and links C and C++ through all three documented compiler prefixes
and representative RV32I, RV32IM, RV32IMC, RV32E, RV32F, RV32D, and
RV64/LP64D multilibs, checks the multilib count, and executes an RV32 ELF under
Spike. The PicoRV32 candidate path provides project-level validation.

The RISC-V RootFS DiffIDs must begin with the exact standard-image DiffID list.
This proves the derived image continues to share the standard layers instead of
silently copying or rebuilding them.

## Evidence and size policy

[`image_size_report.py`](../../.github/scripts/image_size_report.py) records
these metrics independently as exact integer bytes:

- ordered compressed layer descriptors from the selected pushed platform
  manifest, plus unique content-addressed blob totals;
- Docker image inspect's local `.Size`;
- the sum of unpacked Docker history entries;
- disk usage visible to the image's declared runtime user;
- RootFS and history layer counts; and
- the 25 largest directories visible to that user.

The report also records the platform manifest digest, local image ID, OS,
architecture, runtime user, DiffIDs, timestamp, Docker client/server and
containerd versions, Buildx and BuildKit versions, and storage driver. These
represent different storage views and must not be added together. PR 1 records
baselines but intentionally sets no hard size ceiling; candidate-derived limits
belong to the promotion stage after the optimized images exist.

The committed [0.2.10 Linux/AMD64 baseline](../../.github/evidence/docker-image-baseline-0.2.10-amd64.json)
is the control for the staged image work. Workflow reports retain the complete
per-layer and DiffID arrays; the committed control keeps the exact totals,
digests, environment, counts, and largest-directory inventory used by the plan.

The proposed 3.1–3.4 GB standard and 4.6–4.9 GB RISC-V visible-filesystem
endpoints remain design targets, not measured results or CI limits, until the
candidate images prove them.

This first contract stage is not the complete optimization-promotion gate. It
establishes exact storage evidence, image identity, command/file invariants,
representative compiler and Spike execution, and a real PicoRV32 lint/simulation
run. Before a PR changes the shipped runtime payload, the promotion gate must
also cover the Ibex demo, Spike differential and extension-loading behavior,
agent-client signal and exit propagation, OpenROAD physical execution, cold
start time, and representative peak RSS as specified by the image-size audit.
