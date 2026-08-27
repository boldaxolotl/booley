# External program version audit — 2026-08-27

This note inventories the independently executable third-party programs that
Booley pins, installs, or invokes. It compares repository state with the latest
stable release visible from the publisher on 2026-08-27. "Latest" follows the
publisher's production channel: prereleases are excluded, Node uses LTS rather
than Current, and a deliberately pinned development snapshot is not silently
reclassified as a stable release.

## Conclusion

The routine update set is:

- Yosys `v0.67` → `v0.68`
- sv2v `v0.0.12` → `v0.0.13`
- Verible `v0.0-4080-ga0a8d8eb` → `v0.0-4148-g1ea007ec`
- Claude Code `2.1.234` → `2.1.247`
- Codex CLI `0.147.0` → `0.150.1` (not the `0.151.0-alpha.*` channel)
- Docker CLI in the reaper image `29.6.2-cli` → `29.7.2-cli`
- Rust builder `1.97.1` → `1.98.0`, retaining the Bookworm image variant
- Python 3.13 sidecar images `3.13.14` → `3.13.15`
- NumPy in the curated cocotb runtime `2.5.1` → `2.5.2`
- CI `build` pin `1.3.0` → `1.5.0` and `twine` `6.1.0` → `7.0.0`
- CI `pytest` pin `9.0.2` → `9.1.1`
- refresh the immutable `ubuntu:24.04` image digest without changing the OS
  series

Four upgrades need explicit compatibility or packaging work rather than a
blind bump: OpenROAD 26Q3, Verilator 5.050, Vivado 2026.1, and Node 24 LTS.
Spike needs a release-channel decision because Booley deliberately follows a
tested master commit instead of the obsolete formal release. An Ubuntu 26.04
rebase is likewise a separate platform migration, not a routine digest refresh.

## Managed EDA and HDL programs

| Program | Repository version and location | Latest stable/current official | Assessment |
| --- | --- | --- | --- |
| Yosys, including ABC and the slang/sv-elab submodules | `v0.67` / `2d1509d…` in `src/booley/data/docker/Dockerfile.base` | [`v0.68`](https://github.com/YosysHQ/yosys/releases/tag/v0.68), 2026-08-05 | **Update.** ABC, slang, and sv-elab are recursively fetched Yosys submodules, not separate Booley version pins. |
| OpenROAD | public prebuilt `.deb` `2.0-17598-ga008522d8`, release `2024-12-14`, in `Dockerfile.base` | source tag [`26Q3`](https://github.com/The-OpenROAD-Project/OpenROAD/tree/26Q3), 2026-07-01 | **Packaging decision required.** The current source is materially newer, but the old publisher's [release page](https://github.com/Precision-Innovations/OpenROAD/releases) says binary releases moved to VaultLink. The audit did not establish a current, public, drop-in `.deb` and checksum. Choose a registered binary channel or a reproducible source build before changing the pin. |
| Icarus Verilog / `vvp` | `v13_0` / `dfeee9…` in `Dockerfile.base` | [`v13_0`](https://github.com/steveicarus/iverilog/releases/tag/v13_0), 2026-03-02 | **Current.** `vvp` ships with Icarus and has no independent pin. |
| Verilator | deliberately held at `v5.046` / `24b2ac…` in `Dockerfile.base` | [`v5.050`](https://github.com/verilator/verilator-announce/issues/84), 2026-07-01 | **Validate before updating.** 5.050 added forceable unpacked-array support, relevant to the repository's documented 5.048 regression. However, the publisher also confirmed a [silent shift miscompile in 5.048–5.050](https://github.com/verilator/verilator/issues/7955), fixed after 5.050. Prefer a validated post-fix commit or the next stable release. |
| sv2v | `v0.0.12` in `Dockerfile.base` | [`v0.0.13`](https://github.com/zachjs/sv2v/releases/tag/v0.0.13), 2026-03-20 | **Update.** |
| Verible | `v0.0-4080-ga0a8d8eb` in `Dockerfile.base` | [`v0.0-4148-g1ea007ec`](https://github.com/chipsalliance/verible/releases/tag/v0.0-4148-g1ea007ec), 2026-08-16 | **Update.** Verible publishes rolling, commit-derived releases; this is the latest release, not a semantic-version series. |
| AMD Vivado | accepted host version is exactly `2025.2` in `src/booley/eda/vivado.py` and `src/booley/eda/authority.py` | [Vivado `2026.1`](https://www.amd.com/en/support/downloads/adaptive-socs-and-fpgas/development-tools/2026-1.html), 2026-06-23 | **Compatibility-policy update.** This changes host validation, cache identity, fixtures, documentation, and licensing assumptions; it is not a Docker image pin. Validate the 2026.1 tiered-license behavior and wrapper contract. |
| Edalize | `0.6.8` in `pyproject.toml` and `Dockerfile.base` | [`0.6.8`](https://pypi.org/project/edalize/), 2026-04-24 | **Current.** Booley patches a Verible tool node into this version; keep that patch until upstream behavior is verified. |
| FuseSoC | `2.4.6` in `pyproject.toml` and `Dockerfile.base` | [`2.4.6`](https://github.com/olofk/fusesoc/releases/tag/2.4.6), 2026-05-10 | **Current.** |
| cocotb / `cocotb-config` | `2.0.1` in `Dockerfile.base` | [`2.0.1`](https://github.com/cocotb/cocotb/releases/tag/v2.0.1), 2025-11-15 | **Current.** |
| cocotbext-axi | `0.1.28` in `Dockerfile.base` | [`0.1.28`](https://pypi.org/project/cocotbext-axi/), 2026-03-12 | **Current.** Runtime library rather than a standalone EDA executable, included because it is an exact curated image pin. |
| cocotbext-uart | `0.1.4` in `Dockerfile.base` | [`0.1.4`](https://pypi.org/project/cocotbext-uart/), 2025-09-07 | **Current.** Runtime library rather than a standalone EDA executable, included for the same reason. |
| NumPy | `2.5.1` in `Dockerfile.base` | [`2.5.2`](https://pypi.org/project/numpy/), 2026-08-09 | **Update.** Runtime library rather than a program, included because it is an exact curated cocotb image pin. |

Commercial simulators named in roadmap or incubation material are not current
dependencies. Xcelium and VCS modules parse captured output but explicitly do
not launch those programs. Questa/ModelSim, Design/Fusion Compiler, Genus, HAL,
SpyGlass, Verdi, JasperGold, and Quartus are documentation-only. GHDL is not a
supported backend. `cocotbext-spi` is deliberately absent because its released
API is incompatible with cocotb 2.x.

## RISC-V programs

| Program | Repository version and location | Latest stable/current official | Assessment |
| --- | --- | --- | --- |
| xPack RISC-V GCC | `15.2.0-1` in `src/booley/data/docker/Dockerfile.riscv` | [`15.2.0-1`](https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/tag/v15.2.0-1) | **Current published binary.** An upstream source tag does not supersede the published xPack artifact version. |
| Spike / riscv-isa-sim | tested master snapshot `55b4658dbf574ba0b714083ec436ce2cb5be1998` in `Dockerfile.riscv` | formal release [`v1.1.0`](https://github.com/riscv-software-src/riscv-isa-sim/releases/tag/v1.1.0), 2021-12-17; latest visible master was [`15743408284cfe7027bcfe3cf6a13a3c4c6e1a05`](https://github.com/riscv-software-src/riscv-isa-sim/commit/15743408284cfe7027bcfe3cf6a13a3c4c6e1a05) | **Ambiguous channel.** The stable release is older and the Dockerfile documents that it does not compile with Ubuntu 24.04's GCC. The pinned master snapshot is behind master, but moving it is snapshot validation work—not a stable-version bump. Decide whether the policy is “tested master snapshot” or a maintained fork/release. |
| `srec_cat`, `dtc`, `pdftotext` | unversioned Ubuntu packages `srecord`, `device-tree-compiler`, and `poppler-utils` in `Dockerfile.riscv` | Ubuntu 24.04 archive versions at image-build time | **No static repo comparison.** They float after `apt-get update`; rebuilding the image updates them within the selected Ubuntu archive. Pin an Ubuntu snapshot if exact reproducibility is required. |

The RISC-V ISA, debug, and psABI artifacts in `Dockerfile.riscv` are reference
documents, not programs, and are outside this audit.

## Agent CLIs, runtimes, and container utilities

| Program | Repository version and location | Latest stable/current official | Assessment |
| --- | --- | --- | --- |
| Claude Code | exact npm dependency `2.1.234` in `agent-clis-package.json` and lockfile; asserted by `Dockerfile.base` | [`2.1.247`](https://github.com/anthropics/claude-code/releases/tag/v2.1.247), 2026-08-26 | **Update** package manifest, lockfile, and build assertion together. |
| Codex CLI | exact npm dependency `0.147.0` in the same files | [`0.150.1`](https://github.com/openai/codex/releases/tag/rust-v0.150.1), 2026-08-27 | **Update.** `0.151.0-alpha.*` is a prerelease and is excluded. |
| Node.js | exact publisher tarball `22.23.2` in `Dockerfile.base` | [LTS `24.20.0`](https://nodejs.org/en/about/previous-releases); Current is `26.8.1` | **LTS migration candidate.** `22.23.2` is current on the supported 22.x lane, but Node's production guidance prefers an Active/Maintenance LTS and the newest LTS is 24. Test the two agent CLIs on Node 24 before moving. Do not select non-LTS 26 merely because its version is larger. |
| Python sidecars | `python:3.13.14-slim-bookworm` and `python:3.13.14-alpine3.24` in the egress proxy and reaper Dockerfiles | [Python `3.13.15`](https://www.python.org/downloads/) on the existing minor lane; overall stable is `3.14.7` | **Patch update to 3.13.15.** Treat 3.14 as a separately tested minor-version migration. The base image's deadsnakes `python3.13` package is floating, not an exact patch pin. |
| FlexNet relay Python image | `python:3.13-alpine` frozen by digest in `Dockerfile.flexnet-relay` | the [publisher's image metadata](https://hub.docker.com/layers/library/python/3.13-alpine3.24/images/sha256-42825e7ec3437b3bce923c237484eb23d32128476e18307d2f48951bf86f1db2) resolves that index digest to Python `3.13.15` / Alpine 3.24 | **Current patch payload.** Despite the generic-looking tag, the digest makes it immutable. Consider spelling the full patch/Alpine tag for clarity when refreshing it. |
| Rust / Cargo builder | `rust:1.97.1-slim-bookworm` in `src/booley/data/docker/Dockerfile` | [Rust `1.98.0`](https://blog.rust-lang.org/releases/), 2026-08-20 | **Update**, retaining Bookworm because the Dockerfile documents a glibc compatibility requirement. The built `bwave` program itself is repository-native and has no external upstream version. |
| Docker CLI copied into the reaper | `docker:29.6.2-cli` in `Dockerfile.reaper` | [Docker `29.7.2`](https://docs.docker.com/engine/release-notes/29/#2972), 2026-08-05 | **Update.** This is distinct from the unpinned host Docker prerequisite. |
| Ubuntu runtime base | `ubuntu:24.04` frozen by digest in `Dockerfile.base` | the [official image](https://hub.docker.com/_/ubuntu) currently publishes `24.04` as `noble-20260810`; [Ubuntu 26.04](https://releases.ubuntu.com/) is the newest LTS series | **Refresh the 24.04 digest.** A 26.04 rebase is a separate migration: the Dockerfile's Python, compiler, and OpenROAD compatibility assumptions explicitly target 24.04. |

No media codec suite is installed or invoked. Searches found no FFmpeg/
`ffprobe`, x264/x265, AV1, Opus, Vorbis, LAME, FLAC, SoX, or MediaInfo use.
The only media/document-adjacent executable is `pdftotext`, covered above. If
“codecs” meant **Codex**, the Codex CLI is included in the table.

No AWS CLI, Azure CLI (`az`), Google Cloud CLI (`gcloud`/`gsutil`), `s3cmd`,
`rclone`, `kubectl`, Helm, or Terraform is present. The only service CLI is the
optional GitHub CLI: feedback submission uses `gh` if already installed, but
Booley neither installs nor pins it. Its latest stable is
[`2.98.0`](https://github.com/cli/cli/releases/tag/v2.98.0); there is no
repository version to update.

## Development and release CLIs

| Program | Repository version and location | Latest stable/current official | Assessment |
| --- | --- | --- | --- |
| Ruff | `0.16.4` in `pyproject.toml` and `.github/workflows/test.yml` | [`0.16.4`](https://pypi.org/project/ruff/) | **Current.** |
| pytest-cov | `7.1.0` in `pyproject.toml` | [`7.1.0`](https://pypi.org/project/pytest-cov/) | **Current.** |
| pytest | exact host-side CI pin `9.0.2`; the development dependency is floating `>=8.0` | [`9.1.1`](https://pypi.org/project/pytest/), 2026-06-19 | **Update the CI pin.** The suite already runs on pytest 9; validate the 9.1 patch release before merging. |
| PyPA build | `.github/workflows/test.yml` and `publish.yml` pin `1.3.0`; `docker-publish.yml` pins `1.5.0` | [`1.5.0`](https://pypi.org/project/build/), 2026-04-30 | **Update the 1.3.0 CI pins and align workflows.** `1.5.1` is yanked for breaking changes, so it is excluded. |
| Twine | `.github/workflows/test.yml` and `publish.yml` pin `6.1.0` | [`7.0.0`](https://pypi.org/project/twine/), 2026-07-27 | **Update both CI pins.** |

GitHub Actions such as `actions/checkout`, `setup-python`, and Rust toolchain
actions are automation components pinned by commit, not programs installed or
invoked by Booley itself; they are excluded from this program-version audit.
Transitive Python, npm, Cargo, and action dependencies are also excluded unless
they are an explicit runtime/toolchain pin listed above.

## Floating and host-provided programs

These programs are genuinely used, but the repository has no version to
compare. Their presence should not be misreported as “already current.”

- Ubuntu `apt` installs Git, curl, Make, GCC/G++, CMake, gawk, pkg-config,
  autoconf, flex, bison, gperf, help2man, ripgrep, unzip, xz-utils, jq,
  Bubblewrap, and supporting libraries without package versions. The exact
  result floats with Ubuntu/deadsnakes repositories at build time.
- The host must provide Docker. A few abstractions mention Podman, but current
  operational paths and Doctor checks use Docker. No minimum host version is
  encoded.
- Host Git and Bash are directly invoked. Cargo may be used as a host fallback
  in development. Windows paths can invoke `cmd`, PowerShell, and `taskkill`.
- Editor discovery can invoke `code`, `code-insiders`, `codium`, `cursor`, or
  `windsurf`. VaporView (`lramseyer.vaporview`) is installed through an
  unversioned editor-extension identifier. These are host integrations, not
  image pins.
- User-configured authentication mint commands and project-defined hooks/EDA
  commands are arbitrary executables by design and cannot be inventoried as
  Booley dependencies.

If exact rebuild reproducibility is a goal, the apt surface should be changed
to a dated Ubuntu snapshot (or recorded from the built image) rather than
attempting to track every package's unrelated upstream “latest” release.
