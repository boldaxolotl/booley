# Node 24, Python 3.14, and Ubuntu 26.04 migration test plan

This is the execution plan for
[#156](https://github.com/boldaxolotl/booley/issues/156). It records the
original 31 AUG 2026 matrix, the disposition of each attempted phase, and the
smaller matrix that remains. It does not itself change a production pin or
close #156.

Use these terms consistently:

- **Passed** means every required gate ran and passed.
- **Held** means the candidate was not promoted because a prerequisite or gate
  did not pass.
- **Promoted by waiver** means a named gate did not pass or run and a
  maintainer explicitly accepted that exception. A waiver is not a pass.

## Historical baseline and matrix

This section preserves the original plan based on `main` at
`a1c11fbf5a93cec758c779be9629cfb23fa3df48` (`v0.2.9`). It is historical
context, not instructions for constructing new candidates.

The original baseline had these immutable inputs:

| Surface | Historical baseline |
| --- | --- |
| Session Runtime | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Session Python | CPython 3.13 from deadsnakes |
| Node.js | 22.23.2, tarball SHA-256 `d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307` |
| Agent CLIs | Claude Code 2.1.251; Codex CLI 0.151.0 |
| Debian sidecar | `python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca` |
| Alpine sidecars | `python:3.13.15-alpine3.24@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d` |

The original candidates were:

| ID | Ubuntu | Session Python | Node | Sidecars | Historical purpose |
| --- | --- | --- | --- | --- | --- |
| `B` | 24.04 | 3.13 | 22.23.2 | 3.13.15 | Fresh control |
| `N` | 24.04 | 3.13 | 24.20.0 | 3.13.15 | Isolate Node and both agent CLIs |
| `S` | 24.04 | 3.14 | 22.23.2 | 3.13.15 | Isolate Session Python and Booley dependencies |
| `P` | 24.04 | 3.13 | 22.23.2 | 3.14.7 | Isolate all three sidecars |
| `U` | 26.04 | distro Python 3.14 | 22.23.2 | 3.13.15 | Isolate OS/compiler effects against `S` |
| `F` | 26.04 | distro Python 3.14 | 24.20.0 | 3.14.7 | Detect interactions after `N`, `S`, `P`, and `U` |

The plan originally required all six images to use the same source commit and
`linux/amd64` platform. `B` was to be rebuilt so cache state, archive drift,
and measurement method matched the candidates. Node 24.20.0, Python 3.14.7,
and Ubuntu 26.04 were to come from their official release channels with every
archive checksum and OCI digest recorded. Claude Code, Codex CLI, cocotb, EDA
tools, and the Rust builder were to remain unchanged so failures could be
attributed to one runtime variable.

Do not use `B`, `N`, `S`, `P`, `U`, or `F` as a recipe for resumed work.
Node, the sidecars, and OpenROAD have since changed on production `main`;
reconstructing the old rows would test inputs that are no longer candidates.

## Recorded execution outcomes

The following dispositions were reached on 31 AUG–1 SEP 2026:

| Historical row | Delivery | Disposition | Recorded result |
| --- | --- | --- | --- |
| `N` — Node 24 | [PR #205](https://github.com/boldaxolotl/booley/pull/205); [evidence](runtime-migration-node24-evidence-20260831.md) | **Promoted by waiver** | Same-commit control/candidate builds, size comparisons, policy probes, PicoRV32, and Spike flows passed. The authenticated provider matrix did not pass: Anthropic required workspace selection and OpenAI lacked API credits. The maintainer [explicitly waived that matrix](https://github.com/boldaxolotl/booley/pull/205#issuecomment-5484003578) for this promotion. |
| `S` — Session Python 3.14 | [PR #207](https://github.com/boldaxolotl/booley/pull/207); [evidence](runtime-migration-python314-evidence-20260831.md) | **Held** | The existing Noble/deadsnakes channel could not supply the selected Python 3.14.7 patch, so the same-patch `S`/`U` precondition could not be met. No Session-Python production pin changed. |
| `P` — Python 3.14 sidecars | [PR #208](https://github.com/boldaxolotl/booley/pull/208); [evidence](runtime-migration-sidecar-python314-evidence-20260831.md) | **Passed and promoted** | The same-distribution proxy, FlexNet, and reaper candidates passed their no-cache builds, behavior and hardening checks, image-owned E2Es, and size/history review. |
| `U` — Ubuntu 26.04 | [PR #211](https://github.com/boldaxolotl/booley/pull/211); [evidence](runtime-migration-ubuntu2604-evidence-20260831.md) | **Held** | Ubuntu 26.04 supplied Python 3.14.4 while the live Noble control supplied 3.14.6. The exact-patch control could not be constructed, so the compiler/OS matrix did not run and Ubuntu 24.04 stayed in production. |
| `F` — original combination | — | **Not attempted** | `S` and `U` did not clear their prerequisites. Building `F` would not have produced attributable evidence. |

[PR #201](https://github.com/boldaxolotl/booley/pull/201) separately selected
and promoted the digest-pinned OpenROAD 26Q3 OCI channel; the exact decision
and provenance are retained in the
[channel evidence](openroad-26q3-channel-20260831.md). Consequently, production
`main` already contains Node 24.20.0, Python 3.14.7 sidecars, and the selected
OpenROAD artifact. It still uses Ubuntu 24.04 and Session Python 3.13.

The #205 waiver is narrowly scoped to the eight authenticated Anthropic/OpenAI
turns for that Node promotion. It does not convert those turns into passes and
does not cover `S2`, `U2`, or a later interaction candidate. Authenticated
checks for each new candidate must run or receive a new maintainer waiver that
names the candidate and omitted gates.

## Resumed matrix

At execution time, select one then-current production `main` commit and record
its full object ID. Build every resumed candidate from that exact source
commit. The closure update itself was prepared against production `main` at
`96a0b3f38f32f8ce1d7184973045c376f49d271f`; this is a status anchor, not a
permanent candidate pin.

| ID | Definition | Purpose |
| --- | --- | --- |
| `C2` | Fresh rebuild of then-current production `main` | Control for resumed work |
| `S2` | `C2`, changing only Session Python to the selected 3.14 patch | Isolate Session Python |
| `U2` | `S2`, changing only Ubuntu to 26.04 while keeping the identical Python patch | Isolate OS/compiler effects |

`C2`, `S2`, and `U2` retain the production Node 24.20.0 pin, Python 3.14.7
sidecars, agent CLI versions, EDA pins, Rust builder, and OpenROAD 26Q3 source
and OCI digest. Because the promoted Node, sidecar, and OpenROAD inputs are
already present, `U2` is also the combined candidate. Add a final candidate
only if `S2` or `U2` exposes an interaction that requires a separate code
change; define that candidate as a one-change delta from the row that exposed
the interaction.

Choose the Session-Python patch by starting with the Python patch supplied
natively by Ubuntu 26.04 at execution time. `S2` must obtain that identical
upstream patch through the existing supported Ubuntu 24.04 channel. If it
cannot, record a hold and stop before building `U2`; do not change Python
source, add a private build, or mix distributions merely to complete the
matrix.

## Common build and evidence contract

Use disposable candidate Dockerfiles or narrowly scoped build arguments. Do
not edit all production pins first. Build `C2`, `S2`, and `U2` for
`linux/amd64`, from the selected source commit, with equivalent commands and
empty caches for the stable base, final Session Image, and RISC-V flavor.

For every candidate retain:

- source commit, complete build command, target platform, start/end times, and
  proof of empty-cache execution;
- `docker image inspect`, `docker history --no-trunc`, package inventories,
  version output, and test logs;
- every external `FROM` digest and downloaded archive checksum;
- uncompressed `.Size` bytes for the stable base, final Session Image, and
  RISC-V image; and
- a dated evidence report beside this plan with direct links to the delivery
  PR and hosted evidence.

Resolve moving OCI tags to `sha256:` digests, use the digest in the candidate,
and retain the matching `RepoDigests`. Keep `npm ci` and committed lockfile
integrities. Do not change Claude Code, Codex CLI, EDA-tool revisions, cocotb,
sidecar inputs, or the Rust builder during this matrix.

## Symmetric attribution gates

Run every applicable probe below on all three rows. `S2` is compared with `C2`
for Session-Python effects; `U2` is compared with `S2` for Ubuntu/compiler
effects. A probe used to attribute a `U2` result must have a corresponding `S2`
result from the same source commit and evidence method. Missing `S2` evidence
holds `U2`; a `C2` comparison cannot substitute for it.

| Evidence surface | Required symmetric proof |
| --- | --- |
| Image builds | Empty-cache stable-base, final Session, and RISC-V builds for `C2`, `S2`, and `U2` |
| Python runtime | Exact patch/version assertions, full dependency resolution logs, `python -m pip check`, curated runtime imports, agent-SDK discovery, and user-site path checks |
| Simulation | Equivalent plain Icarus and cocotb/Icarus simulations, Icarus/cocotb VPI discovery, and Verilator/FST cross-validation |
| Synthesis and conversion | Yosys plus ABC and `read_slang` builds and representative synthesis; checksum/version and real conversion probes for sv2v; Verible lint and conversion probes |
| OpenROAD | The selected OCI artifact's digest/provenance, source sentinel, loader and glibc checks, `openroad -version`, and a real Nangate45 physical synthesis, placement, and timing flow |
| Product flows | Final-image validation, sandbox isolation, FIFO pipeline, Ticket Mode image smoke, PicoRV32 readiness/demo and differential flows, and Spike differential flows |
| Native compatibility | Package inventories, native-payload discovery behind wrappers, `file`, `readlink -f`, `ldd`, ELF/glibc symbol ceilings, and execution of each payload's real smoke |
| Image construction | `.Size` and `docker history --no-trunc`, named-layer attribution, and checks for duplicate interpreters/toolchains, package caches, and build-only payloads |

### Python and simulator gate

Retain resolver output for the complete dependency install in each row. Run
`python -m pip check`, direct imports of every curated runtime dependency, and
SDK discovery of the system Claude executable. Run both plain and cocotb
Icarus simulations so compiled extensions and the simulator bridge execute
rather than merely import. Verify the VPI library path and repeat the native
Verilator/FST ground-truth suite. A skipped compatibility test is not a pass.

`S2` passes only when its behavior matches `C2` apart from the selected Python
change. `U2` passes this gate only when it matches the corresponding `S2`
result apart from explained Ubuntu/compiler effects.

### EDA build and flow gate

An empty-cache build is required for every row; a cached layer is not compiler
evidence. Require each candidate to:

- build Yosys, ABC, and the `read_slang` frontend from the pinned revisions and
  run representative SystemVerilog synthesis;
- build Icarus and run plain SystemVerilog plus the cocotb VPI smoke;
- build Verilator and pass the native FST cross-validation and simulator
  ground-truth suite;
- verify the pinned sv2v and Verible archives, then run actual conversion and
  lint probes;
- pass final-image Verible, sandbox, FIFO, Ticket Mode, and Nangate45 physical
  flow smokes; and
- build the RISC-V flavor and pass PicoRV32 and Spike differential flows.

Review warnings introduced by the newer compiler. Do not globally suppress
them. Put any source compatibility patch in a separate commit with an upstream
reference and regression test.

### Agent-policy and authenticated gate

For both installed CLIs, assert exact versions, diagnostic entry points,
recursive npm-tree and lockfile integrity, generated Booley configuration, and
the offline hostile-configuration matrix. Codex must reject or effectively
disable web search requested by user configuration, trusted-project
configuration, command-line configuration, `--search`, and
danger-full-access. Claude must omit `WebFetch` and `WebSearch` under user,
project, CLI-allow, and bypass-permissions attempts while retaining and
successfully invoking a permitted canary tool.

Run one minimal authenticated turn through each direct CLI and each Booley
backend on `C2`, `S2`, and `U2`. Use fresh disposable homes and an empty
synthetic repository. A provider login failure, workspace-selection prompt,
or exhausted credit means the gate did not pass. Promotion then requires a new
explicit maintainer waiver scoped to the affected candidate and exact turns;
the #205 waiver cannot be reused.

Use dedicated, short-lived, least-privilege credentials. Mount credentials as
read-only runtime secrets and export them only to the child process from an
untraced wrapper. Never place a secret in a command argument, build argument,
image layer, `docker run -e NAME=value` metadata, repository file, or retained
raw transcript. Run with `--rm`, disposable homes, and cleanup traps. Revoke
credentials after the matrix and run the repository confidential-content
guard plus a test-credential fingerprint scan before uploading an allowlisted
summary.

### OpenROAD gate

The Precision Innovations 2024-12-14 Ubuntu 22.04 `.deb` probe is historical
evidence only. Its `libpython3.10` dependency and Ubuntu 26.04 installation
failure remain useful context, but it is not the production artifact and is
not a current gate.

The current input selected by
[#201](https://github.com/boldaxolotl/booley/pull/201) is the official
OpenROAD 26Q3-source OCI index:

```text
docker.io/openroad/ubuntu24.04@sha256:c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a
```

It resolves to source commit
`a9147cf3aebe65e058bb3fa89c1f9e524488dbb8`. Keep that source identity and
binary channel identical across `C2`, `S2`, and `U2`, adapting only the
container integration needed for the Ubuntu row. For each row verify the
index/platform digest and provenance, embedded source sentinel, binary banner,
license/source record, complete `ldd` resolution, glibc ceiling, and a real
Nangate45 physical flow. Do not substitute the old `.deb`, a VaultLink
package, or an unpinned source build. Issue #154 was closed by #201; resumed
work must not wait on it.

### Native compatibility gate

Capture `getconf GNU_LIBC_VERSION`, `ldd --version`, and
`readelf --version-info` for native executables and Python extensions. At
minimum inspect Node, both agent-CLI native payloads, B-Wave, Yosys/ABC,
OpenROAD, Icarus/vvp, Verilator, sv2v, Verible, and compiled Python extensions.
Resolve wrappers and symlinks with `readlink -f` and `file`; inspecting only a
launcher script is insufficient.

Fill this table with measured values in the resumed evidence report:

| Artifact class | Maximum allowed requirement |
| --- | --- |
| B-Wave copied from the Bookworm builder | No newer than `GLIBC_2.34` |
| Node, Claude/Codex native payloads, and pinned prebuilt EDA artifacts shared by all rows | No newer than the glibc recorded in `C2` |
| Python extensions and EDA binaries built inside `C2` or `S2` | No newer than that row's runtime glibc |
| Python extensions and EDA binaries built inside `U2` | No newer than `U2`'s runtime glibc; never copy them into an older row |

Every ELF object must resolve all loaders and libraries and execute its real
smoke. A symbol above the applicable ceiling, an unresolved library, an
unidentified native payload, or a newer-image artifact copied into an older
runtime fails the gate.

### Image-size and inventory gate

Size is decision evidence, not a compatibility budget. Compare both image
`.Size` and full history for only these single-variable pairs:

- `S2 - C2`: stable base, final Session, and RISC-V flavor, attributing the
  Session-Python change; and
- `U2 - S2`: the same three images, attributing Ubuntu/compiler effects.

Report absolute and percentage deltas and trace every changed byte range to a
named layer. Confirm that Node 24, all sidecar images, OpenROAD, agent CLIs,
EDA pins, and the Rust builder are identical across the rows. Missing
measurements, mixed-variable comparisons, duplicate Python/Node/Rust
toolchains, retained package caches, or build-only files in a final image fail
the gate.

## Promotion, delivery, and issue disposition

Record a hold as soon as a prerequisite fails; do not weaken an assertion or
add a second migration variable to make a candidate green. `S2` may be
promoted only after its complete comparison with `C2` passes or every omitted
gate has a new explicit waiver. `U2` may be promoted only after `S2` is an
accepted control and its symmetric `U2 - S2` matrix passes or receives its own
explicit waiver.

Deliver resumed work as reviewable PRs based on then-current `main`, normally
one for `S2` and one for `U2`. A separate final PR is needed only for an
interaction-specific change. Each PR and evidence report must say `Refs #156`
and link its exact source commit, candidate inputs, results, and disposition.

Run the repository checks applicable to every changed file, including
`git diff --check`. For Python changes also run:

```text
ruff check src/ tests/
ruff format --check src/ tests/
```

Keep #156 open while Session Python or Ubuntu remains held. Close it only after
the resumed matrix has a linked, reviewable disposition for every remaining
runtime surface and the accepted production state is recorded without calling
a waiver a pass.
