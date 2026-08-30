# External program version audit - 2026-08-31

This audit covers every executable, compiler, runtime, immutable image, and
build or publication tool that `origin/main` at
`d674b9cf853293860f912e2a5abe38c9279e42ff` pins or invokes. Publisher
production channels were queried on 2026-08-31. Prereleases were excluded.

Four stable releases appeared after the 2026-08-28 refresh and are routine
updates in this refresh:

- Verible `v0.0-4148-g1ea007ec` to `v0.0-4157-gfdbac312`
- Claude Code `2.1.250` to `2.1.251`
- Codex CLI `0.150.1` to `0.151.0`
- `softprops/action-gh-release` `v3.0.2` to `v3.0.3`

Cocotb `2.1.0` also appeared. Its scheduler and simulator-library changes
affect Booley's Icarus and Verilator runtime contract, so it is held for the
compatibility work tracked in [#188](https://github.com/boldaxolotl/booley/issues/188).

## EDA, simulation, synthesis, and lint tools

| Program | Installed pin before this refresh | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Yosys, ABC, and slang | `v0.68` / `38e001a6...`; ABC and slang follow its submodules | `v0.68` | [Yosys v0.68](https://github.com/YosysHQ/yosys/releases/tag/v0.68) | **current** |
| OpenROAD | `2.0-17598-ga008522d8`, public `2024-12-14` Debian package | `26Q3` source tag | [OpenROAD 26Q3](https://github.com/The-OpenROAD-Project/OpenROAD/tree/26Q3) | **held**: current binary distribution moved away from the public release channel, and no reproducible drop-in package and checksum are established. Follow-up: [#154](https://github.com/boldaxolotl/booley/issues/154). |
| Icarus Verilog and `vvp` | `v13_0` / `dfeee909...` | `v13_0` | [Icarus v13_0](https://github.com/steveicarus/iverilog/releases/tag/v13_0) | **current** |
| Verilator | `v5.046` / `24b2ac24...` | `v5.050` | [5.050 announcement](https://github.com/verilator/verilator-announce/issues/84) | **held**: 5.050 contains the documented nested-shift miscompile; the fix landed after the release. [#153](https://github.com/boldaxolotl/booley/issues/153) defines the first acceptable future stable target and validation matrix. |
| sv2v | `v0.0.13` | `v0.0.13` | [sv2v v0.0.13](https://github.com/zachjs/sv2v/releases/tag/v0.0.13) | **current** |
| Verible | `v0.0-4148-g1ea007ec` | `v0.0-4157-gfdbac312` | [Verible release](https://github.com/chipsalliance/verible/releases/tag/v0.0-4157-gfdbac312) | **routine update**: the nine-commit rolling release changes formatter selection, build dependencies, and upstream smoke-test data without a user migration. Update the binary checksum, image build assertion, tests, and supported-tool documentation together. |
| AMD Vivado | accepted host version `2025.2` | `2026.1` | [Vivado 2026.1](https://www.amd.com/en/support/downloads/adaptive-socs-and-fpgas/development-tools/2026-1.html) | **held**: changing the accepted commercial-tool version affects licensing, discovery, cache identity, wrappers, fixtures, and documentation. Follow-up: [#155](https://github.com/boldaxolotl/booley/issues/155). |
| Edalize | `0.6.8` | `0.6.8` | [PyPI](https://pypi.org/project/edalize/) | **current** |
| FuseSoC | `2.4.6` | `2.4.6` | [FuseSoC 2.4.6](https://github.com/olofk/fusesoc/releases/tag/2.4.6) | **current** |
| cocotb / `cocotb-config` | `2.0.1` | `2.1.0` | [cocotb release notes](https://docs.cocotb.org/en/development/release_notes.html) | **held**: 2.1 changes scheduler behavior, simulator library naming, and the Icarus GPI library extension used by the image assertion. Follow-up: [#188](https://github.com/boldaxolotl/booley/issues/188). |
| cocotbext-axi | `0.1.28` | `0.1.28` | [PyPI](https://pypi.org/project/cocotbext-axi/) | **current** |
| cocotbext-uart | `0.1.4` | `0.1.4` | [PyPI](https://pypi.org/project/cocotbext-uart/) | **current** |
| NumPy | `2.5.2` | `2.5.2` | [PyPI](https://pypi.org/project/numpy/) | **current** |

Commercial simulators and EDA suites mentioned only in roadmap, parsing, or
incubation material are not invoked dependencies. Xcelium and VCS integrations
parse captured output but do not launch those tools. GHDL, Questa/ModelSim,
Design/Fusion Compiler, Genus, HAL, SpyGlass, Verdi, JasperGold, and Quartus are
documentation-only or unsupported.

## RISC-V tools

| Program | Installed pin | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| xPack RISC-V GCC | `15.2.0-1` | `15.2.0-1` | [xPack release](https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/tag/v15.2.0-1) | **current** |
| Spike / riscv-isa-sim | tested master snapshot `55b4658d...` | formal release `v1.1.0`; upstream master `c09c0cce...` is newer | [Spike releases](https://github.com/riscv-software-src/riscv-isa-sim/releases/tag/v1.1.0) | **held**: `v1.1.0` does not build with Ubuntu 24.04's compiler, while moving an arbitrary snapshot requires a channel decision and full RISC-V validation. Follow-up: [#157](https://github.com/boldaxolotl/booley/issues/157). |
| `srec_cat`, `dtc`, `pdftotext` | Ubuntu 24.04 archive packages | Ubuntu 24.04 archive versions at build time | [Ubuntu packages](https://packages.ubuntu.com/) | **unversioned**: apt resolves these within the selected image archive; the repository has no package-version pin. |

The RISC-V ISA, debug, and psABI PDFs and HTML archives are reference data,
not executable tools. Their immutable checksums remain unchanged.

## Agent CLIs, runtimes, compilers, and images

| Program or image | Installed pin before this refresh | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Claude Code | `2.1.250` | `2.1.251` | [Claude Code v2.1.251](https://github.com/anthropics/claude-code/releases/tag/v2.1.251) | **routine update**: the patch includes sandbox, symlink, managed-settings, worktree, and MCP reliability fixes without a migration. Update the npm manifest, lockfile, and image assertions together. |
| Codex CLI | `0.150.1` | `0.151.0` | [Codex 0.151.0](https://github.com/openai/codex/releases/tag/rust-v0.151.0) | **routine update**: the stable minor adds optional MCP discovery and plugin hooks, and fixes sandbox, permission, and tool-routing behavior without a migration. Update the npm manifest, lockfile, and image assertion together. |
| Node.js | `22.23.2` | `22.23.2` on the retained 22.x lane; newest LTS is `24.20.0` | [Node release index](https://nodejs.org/dist/index.json) | **held**: the exact supported lane is current, while Node 24 requires agent-CLI policy and runtime compatibility testing. Follow-up: [#156](https://github.com/boldaxolotl/booley/issues/156). |
| Python sidecars | `3.13.15` on Bookworm and Alpine 3.24, exact digests | `3.13.15` on the retained lane; overall stable is `3.14.7` | [Python 3.13.15](https://www.python.org/downloads/release/python-31315/) | **held**: the exact 3.13 lane and digests are current; a 3.14 sidecar move is a runtime migration tracked by [#156](https://github.com/boldaxolotl/booley/issues/156). |
| Rust/Cargo builder | `rust:1.98.0-slim-bookworm@sha256:1469a27c...` | Rust `1.98.0`; tag digest matches | [Rust 1.98.0](https://blog.rust-lang.org/releases/latest/) | **current** |
| Scheduled fuzz compiler | `nightly-2026-08-20` | nightly is not a stable release channel | [Rust toolchains](https://rust-lang.github.io/rustup/concepts/toolchains.html) | **held**: this immutable nightly is a tested fuzzing snapshot. Move it only for a cargo-fuzz or LLVM requirement. |
| Docker CLI in reaper | `docker:29.7.2-cli@sha256:000bb62f...` | `29.7.2`; tag digest matches | [Docker 29.7.2](https://github.com/moby/moby/releases/tag/docker-v29.7.2) | **current** |
| Ubuntu runtime base | `ubuntu:24.04@sha256:33ceb719...` | exact 24.04 digest matches; newest LTS series is 26.04 | [Ubuntu releases](https://releases.ubuntu.com/) | **held**: the supported 24.04 image is current. A 26.04 rebase changes compiler, Python, OpenROAD, and EDA assumptions and is tracked by [#156](https://github.com/boldaxolotl/booley/issues/156). |
| Egress proxy image | `python:3.13.15-slim-bookworm@sha256:c45a22ea...` | exact tag digest matches | [official Python image](https://hub.docker.com/_/python) | **current** |
| FlexNet relay and reaper images | `python:3.13.15-alpine3.24@sha256:540c7d91...` | exact tag digest matches | [official Python image](https://hub.docker.com/_/python) | **current** |

## Development, build, test, and publication tools

| Tool | Installed pin before this refresh | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Ruff | `0.16.5` | `0.16.5` | [PyPI](https://pypi.org/project/ruff/) | **current** |
| pytest | CI `9.1.1`; project lane `>=8.0` | `9.1.1` | [PyPI](https://pypi.org/project/pytest/) | **current** |
| pytest-asyncio | CI `1.4.0`; project lane `>=0.23` | `1.4.0` | [PyPI](https://pypi.org/project/pytest-asyncio/) | **current** |
| pytest-cov | `7.1.0` | `7.1.0` | [PyPI](https://pypi.org/project/pytest-cov/) | **current** |
| pytest-xdist | supported lane `>=3.8` | `3.8.0` | [PyPI](https://pypi.org/project/pytest-xdist/) | **current** |
| pytest-timeout | supported lane `>=2.3` | `2.4.0` | [PyPI](https://pypi.org/project/pytest-timeout/) | **current**: Booley deliberately supports a compatible release lane rather than an exact pin. |
| coverage.py | resolved through pytest-cov | `7.16.0` | [PyPI](https://pypi.org/project/coverage/) | **unversioned**: there is no direct repository pin. |
| diff-cover | `10.5.1` | `10.5.1` | [PyPI](https://pypi.org/project/diff-cover/) | **current** |
| Pyright | `1.1.411` | `1.1.411` | [PyPI](https://pypi.org/project/pyright/) | **current** |
| mutmut | `3.7.0` | `3.7.0` | [PyPI](https://pypi.org/project/mutmut/) | **current** |
| cargo-fuzz | `0.13.2` | `0.13.2` | [cargo-fuzz 0.13.2](https://github.com/rust-fuzz/cargo-fuzz/releases/tag/0.13.2) | **current** |
| actionlint | `1.7.12` plus exact SHA256 | `1.7.12` | [actionlint v1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12) | **current** |
| setuptools build backend | supported lane `>=84.0.0` | `84.0.0` | [PyPI](https://pypi.org/project/setuptools/) | **current** |
| PyPA build | workflow pin `1.6.0`; project lane `>=1.0` | `1.6.0` | [PyPI](https://pypi.org/project/build/) | **current** |
| Twine | `7.0.0` | `7.0.0` | [PyPI](https://pypi.org/project/twine/) | **current** |
| `softprops/action-gh-release` | `v3.0.2` / `3d0d9888...` | `v3.0.3` / `efb35369...` | [v3.0.3](https://github.com/softprops/action-gh-release/releases/tag/v3.0.3) | **routine update**: the maintenance release updates dependencies and fixes malformed GitHub API error classification without an input migration. |

The other immutable GitHub Actions pins resolve to their current stable
releases: checkout `v7.0.1`, setup-python `v7.0.0`, upload-artifact `v7.0.1`,
download-artifact `v8.0.1`, setup-buildx `v4.3.0`, login `v4.6.0`, build-push
`v7.3.0`, rust-cache `v2.9.2`, and `gh-action-pypi-publish` `v1.14.2`.
The pinned `dtolnay/rust-toolchain` commit follows its production `stable`
channel. Outcome: **current**.

## Host-provided and absent categories

Host Docker, Git, Bash, Cargo, GitHub CLI, Windows command tools, PowerShell,
editors, project hooks, user-configured credential commands, and host EDA
executables other than the exact Vivado acceptance contract have no repository
version pin. Outcome: **unversioned**.

Ubuntu apt installs compilers and utilities without individual versions. They
float only within the immutable Ubuntu image snapshot at build time. Outcome:
**unversioned**.

Repository search found no FFmpeg/`ffprobe`, codec suite, AWS/Azure/Google
Cloud CLI, `kubectl`, Helm, Terraform, or independently pinned service/cloud
CLI. No update is applicable for those absent categories.
