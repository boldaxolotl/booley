# Session Runtime image contract

This contract defines the required behavior of Booley's standard and RISC-V
Session Runtime images. Image-size work may change packaging, layers, debug
symbols, and implementation details. Removing required behavior needs a separately
reviewed contract change.

The executable version of this contract is
[`session-runtime.toml`](../../.github/contracts/session-runtime.toml). CI tests
built images with [`image_contract.py`](../../.github/scripts/image_contract.py)
and retains JSON evidence. Focused image integration tests remain authoritative
for behavior that command and file probes cannot represent.

## Common runtime contract

The image runs as the unprivileged `agent` user (UID/GID 1000), starts in
`/work`, and supports writable project bind mounts owned by that user. Booley
denies network access to Flow subprocesses. The Session Runtime's egress proxy
constrains agent traffic, and system policy disables provider-hosted web tools.

The following command families are required:

- Shell and project compilation: Bash, POSIX `sh`, Git, Make, GCC, and G++.
- Python: Python 3.13, pip, Booley, `booley-mcp`, FuseSoC/Edalize, cocotb, and
  the curated Python dependencies installed by the image recipe.
- Simulation and waveform work: Icarus (`iverilog`, `iverilog-vpi`, `vvp`),
  Verilator's normal, debug, and coverage commands, cocotb's simulator
  libraries, and B-Wave.
- Synthesis and lint: Yosys with ABC and slang, sv2v, OpenROAD, and the complete
  shipped Verible command suite until a later contract review narrows it.
- Agent clients: Node.js, npm, Claude Code, and Codex in their publisher-provided
  launch form.

The image must compile, link, and execute native C and C++ source. It must compile
Verilator-generated C++ with the installed runtime headers. Icarus must retain
its VPI development header, targets, modules, and cocotb VPI library. OpenROAD
must retain its OR-Tools shared-library runtime and run physical flows. A logical
Yosys fallback fails the contract.

Claude and Codex must provide version and help diagnostics, offline startup,
their managed web-isolation policy, normal exit status, and signal propagation.
The Claude Python SDK must resolve the system Claude executable. Its duplicate
bundled executable is intentionally absent. The production-image agent-policy
probe checks publisher package integrity and policy behavior.

The image-level assertions require stripped Yosys, ABC, and sv2v binaries and one
hard-linked inode for the two installed B-Wave paths. Dockerfile text alone does
not satisfy them.

## Rust is not included

The standard Session Runtime excludes Cargo, `rustc`, `rustup`, and the Rust
standard library. A throwaway builder stage compiles B-Wave, and the final image
receives only its runtime binary. Projects that compile Rust need a reviewed
project image with the required pinned toolchain.

The exclusion is deliberate. The Rust toolchain is large, and the installed wheel
cannot rebuild B-Wave because it omits the crate source. Setup guidance must not
claim Rust as a standard-runtime capability.

## RISC-V extension contract

The RISC-V image extends the standard image without replacing its RootFS history.
It adds:

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
This proves that the derived image shares the standard layers and does not copy
or rebuild them.

The promotion path also checks out Ibex at commit
`34b0705760ef3dfa00e99637432473d2be8f22f3` and runs its real `ibex_core` lint
target with FuseSoC and Verilator while container networking is disabled. The
contract's Spike probe runs an independently checked RV32 arithmetic program
and loads a temporary native extension library; this covers both ISA execution
and the installed extension-loading interface.

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
containerd versions, Buildx and BuildKit versions, and storage driver. Do not add
the metrics together; they measure different storage views.

[`image-size-limits.toml`](../../.github/contracts/image-size-limits.toml) sets
exact-byte ceilings for all four storage views. Local PR builds enforce the
three locally measurable views; release builds additionally enforce compressed
registry-layer bytes after pushing the selected platform manifest. Missing
images and breached limits fail closed.

The committed [0.2.10 Linux/AMD64 baseline](../../.github/evidence/docker-image-baseline-0.2.10-amd64.json)
controls the staged image work. Workflow reports retain the complete per-layer
and DiffID arrays. The committed control keeps the plan's exact totals, digests,
environment, counts, and largest-directory inventory.

The clean-runtime candidates measured on 2026-09-04 produced these local
results. They are measurements, not limits:

| Image | Docker local | Unpacked history | Visible filesystem |
| --- | ---: | ---: | ---: |
| standard | 1,577,191,573 B | 3,180,724,224 B | 2,821,029,888 B |
| RISC-V | 2,022,419,977 B | 4,841,689,088 B | 4,480,544,768 B |

The promotion gate now covers the standard and RISC-V command/file contracts,
compiler and simulation paths, PicoRV32 lint/simulation, the pinned offline
Ibex lint demo, Spike execution and extension loading, agent-client signal and
exit propagation, OpenROAD physical placement/timing repair and offscreen GUI
loading, and repeated cold-start and representative peak-RSS measurements.

## Provenance and redistribution evidence

Release builds generate SPDX SBOM attestations for the stable base, standard,
and RISC-V images. OpenROAD is copied from the pinned upstream builder without
its development checkout; the runtime instead carries a deterministic compact
source archive and a machine-readable manifest containing the exact root and
recursive submodule revisions. Required license material remains in the image.

These artifacts provide engineering evidence for source correspondence and
review. They do not replace a human legal review of redistribution obligations
before a release is published.
