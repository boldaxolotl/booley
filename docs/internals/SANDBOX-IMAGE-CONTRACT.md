# Sandbox Image contract

This contract lists what Booley's standard and RISC-V
[Sandbox Images](../GLOSSARY.md) must keep doing, and why. Read it before you
change an image recipe under `src/booley/data/docker/`, the image checks in
CI, or the image size limits. Size work may change packaging, layers, debug
symbols, and implementation details freely; removing a required behavior
needs its own reviewed change to this contract. The user-facing tool list is
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md), and pin bumps
follow [dependency-updates.md](agents/dependency-updates.md).

The executable form is [`session-runtime.toml`](../../.github/contracts/session-runtime.toml):
it holds the exact commands, paths, and probes, and
[`image_contract.py`](../../.github/scripts/image_contract.py) runs it against
built images in CI and keeps JSON evidence. This doc gives the intent; when
it and the TOML disagree, fix one of them. Image integration tests under
`tests/docker/` cover behavior a command or file probe cannot express.

## How the images are built

| Recipe | Produces |
| --- | --- |
| `Dockerfile.base` | Runtime base: Ubuntu packages, Python, EDA tools, agent clients, managed policies. |
| `Dockerfile.substrate` | Standard substrate: the base plus the standard tool layer, with no Booley wheel. |
| `Dockerfile.riscv` | RISC-V substrate: the standard substrate plus the RISC-V tooling. |
| `Dockerfile.wheel` | Final image: any substrate plus the checkout's Booley wheel and B-Wave. |

One wheel recipe serves every substrate: standard, RISC-V, or a Project's
own. `Dockerfile.wheel` also owns the B-Wave binary: its
package-data hard link must be created in the same layer as the binary,
because a layer diff cannot express a link to a lower layer.

## Common runtime

The image runs as the unprivileged `agent` user (UID and GID 1000), starts in
`/work`, and supports writable project bind mounts owned by that user.
Both agent clients can offer provider-hosted web tools whose requests leave
from the provider's servers, outside the Sandbox network. The system policy
files (`claude-managed-settings.json`, `codex-requirements.toml`) turn them
off, and user or project config cannot turn them back on.

Required command families:

- **Shell and native builds:** Bash, POSIX `sh`, Git, Make, ccache, GCC, and
  G++. The image must compile, link, and run native C and C++, including
  Verilator-generated C++ against the installed runtime headers.
- **GNU core utilities by default:** unprefixed core utilities resolve to GNU
  coreutils in login and non-login shells, so Flow scripts, Make runs, and
  hooks behave like a typical Linux host. Ubuntu's uutils selector stays
  installed because `build-essential` depends on it; the image installs
  `gnu-coreutils` and links its programs into `/usr/local/bin`, which comes
  before `/usr/bin` on the default `PATH`. The probe checks GNU identity in
  non-login Bash, login Bash, interactive login Bash, and POSIX `sh`.
  Project-derived and RISC-V images inherit this; a Project may still
  override commands or `PATH`.
- **Python:** Python 3.14, pip, Booley, `booley-mcp`, FuseSoC and Edalize,
  cocotb, and the curated dependencies from the recipe.
- **Simulation and waveforms:** Icarus (`iverilog`, `iverilog-vpi`, `vvp`)
  with its VPI header, targets, modules, and cocotb's VPI library; Verilator's
  normal, debug, and coverage programs; cocotb's simulator libraries; B-Wave.
- **Synthesis and lint:** Yosys with ABC and slang, sv2v, OpenROAD with its
  OR-Tools runtime (it must run a physical flow, not just start), and every
  shipped Verible program.
- **Agent clients:** Node.js, npm, Claude Code, and Codex as the publishers
  ship them. Both clients must print version and help offline, load their
  managed web policy, and exit 0. The Claude Python SDK must find the system
  `claude`; its bundled duplicate is removed to save space. The agent-policy
  probe (`tests/docker/agent_policy_probe.py`) also checks package integrity.

The probe pins (Verilator, cocotb) must match the recipe pins;
`tests/ci/test_sandbox_image_contract.py` checks that.

Some assertions run on the built image because recipe text cannot prove them:
the large binaries (Yosys, ABC, sv2v, Icarus, Verilator, Verible, and Spike in
the RISC-V image) are stripped, and B-Wave's two installed paths share one
inode.

### No Rust toolchain

The standard image has no Cargo, `rustc`, `rustup`, or Rust standard library.
A throwaway builder stage compiles B-Wave and the final image receives only
the binary. The toolchain is about 1.6 GB, and it would not help anyway: the
installed wheel omits the crate source, so B-Wave cannot be rebuilt in the
Sandbox. Setup guidance must not offer Rust as a standard capability; a
Project that compiles Rust needs its own reviewed image with a pinned
toolchain.

## RISC-V image

The RISC-V image is built on the standard substrate and adds:

- the xPack bare-metal compiler under the `riscv-none-elf-` prefix, plus full
  `riscv32-unknown-elf-` and `riscv64-unknown-elf-` aliases for every
  helper program, because Ibex's and PicoRV32's makefiles call those names;
- all 32 publisher multilib entries, including RV32 and RV64 C and C++;
- Spike's four programs and its libraries;
- `srec_cat`, the device-tree compiler, and `pdftotext`; and
- the pinned offline ISA, debug, and psABI reference set under
  `$BOOLEY_RISCV_DOCS`.

CI compiles and links C and C++ through all three prefixes and a set of
representative multilibs from RV32I to RV64/LP64D, checks the multilib
count, runs an RV32 program with a known result under Spike, and loads a
temporary Spike extension library. The PicoRV32 path lints and simulates a
real core, and CI lints Ibex's `ibex_core` at a pinned commit
with container networking disabled, which proves the toolchain works offline.

The RISC-V image's RootFS DiffIDs must start with the standard substrate's
DiffIDs and add at least one layer. That proves it shares the standard
layers instead of copying or rebuilding them.

The `riscv-tooling` stage text is the cache key of the published tooling
image (`.github/scripts/riscv_tooling.py`): any edit to it, even a comment,
rebuilds and republishes that image.

## Size limits and evidence

[`image_size_report.py`](../../.github/scripts/image_size_report.py) records
four storage views as exact bytes: compressed registry layers, Docker's local
`.Size`, the sum of unpacked history layers, and disk usage visible to the
runtime user. It also records layer counts, the largest directories, and the
build environment. Do not add the views together; they measure different
things.

[`image-size-limits.toml`](../../.github/contracts/image-size-limits.toml)
sets a ceiling for each view with about 10% headroom over a clean build. The
ceilings stay below the 0.2.10 baseline
([`docker-image-baseline-0.2.10-amd64.json`](../../.github/evidence/docker-image-baseline-0.2.10-amd64.json),
pinned by `tests/ci/test_image_size_report.py`), so accidentally restoring
the OpenROAD development tree, the Rust toolchain, duplicate agent clients,
or unstripped binaries fails before promotion. PR builds enforce the three
locally measurable views; release builds also enforce compressed bytes after
pushing the platform manifest. A missing image fails the check.

Before promotion, CI also runs OpenROAD placement, timing repair, and
offscreen GUI loading, and records cold-start time and peak memory
(recorded, not gated).

## Provenance

Release builds attach SPDX SBOM attestations to the base, standard, and
RISC-V images. OpenROAD is copied from the pinned upstream builder without its
development checkout; the image instead carries a compact source archive and
a manifest of the exact root and submodule revisions
(`openroad-source-manifest.json`), plus the required license material. These
support source-correspondence review; they do not replace a human legal
review of redistribution obligations before a release.

### Runtime-base package inventory

Every runtime base embeds the package set it resolved at
`/usr/local/share/booley/base-package-inventory.json`, named by the image
label `io.booley.runtime-base.package-inventory`. Apt packages are not
pinned, so two builds of the same commit can differ; the inventory records
what one base actually got. The label is informational:
it is not a freshness or compatibility key, and the image ID or registry
digest stays the identity.

Shape (versions elided):

```json
{
  "packages": [
    {"architecture": "amd64", "name": "libc6", "version": "..."}
  ],
  "python": {"pip_version": "...", "version": "3.14..."},
  "schema": 1,
  "scope": "runtime-base"
}
```

The file has sorted keys, two-space indentation, and one trailing newline.
`packages` lists every dpkg package in state `installed`, sorted by name and
architecture. It carries no timestamp, commit, hostname, or digest, so the
same package set always yields the same bytes.

The helper `/usr/local/libexec/booley/base_package_inventory.py` has two
commands. `generate` writes the file in the base's last layer, after every
apt and pip change, run as `python -I` so user-site packages and `PYTHON*`
variables cannot change it. `validate` checks only the shape (a JSON object
with the expected fields and types and a non-empty `packages` list), not
sorting or live package state; the common contract probe runs it, so an
image without the file fails the contract. Standard, RISC-V, and
Project-derived images inherit the bytes unchanged. The file is not an SBOM
or a lockfile: apt stays floating, so rebuilds are not reproducible, and the
EDA builder stages and derived layers are not listed.

CI extracts the file with `cat` and, where it built the base itself, compares
it byte for byte with the base's copy:

```bash
docker run --rm --network none --entrypoint cat IMAGE \
  /usr/local/share/booley/base-package-inventory.json > inventory.json
cmp parent.json inventory.json
```

CI keeps these artifacts:

| Workflow | Artifact |
| --- | --- |
| `docker-base-publish.yml` | `base-package-inventory-<run>-<attempt>` |
| `test.yml` (base build) | `docker-build-evidence` |
| `test.yml` (`riscv-image` job) | `riscv-image-evidence-*` |
| `docker-publish.yml` | `release-standard-image-*`, `release-riscv-image-*` |
