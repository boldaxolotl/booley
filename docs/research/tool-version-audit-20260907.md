# External program version audit - 2026-09-07

This audit covers every executable, compiler, runtime, immutable image, and
build or publication tool that `origin/main` at
`6bc76138c380a7f817dc410eaf6df2572cb6d26e` pins or invokes. Publisher
production channels were queried on 2026-09-07. Prereleases were excluded.

Five compatible publisher updates are routine refreshes before release 0.2.13:

- Claude Code `2.1.259` to `2.1.263`
- Codex CLI `0.153.1` to `0.153.4`
- NumPy `2.5.2` to `2.5.3`
- PyYAML `6.0.2` to `6.0.3`
- Docker CLI and DinD `29.7.2` to `29.8.0`

Verilator 5.052 is the first stable release that contains Booley's required
compiler fix. That makes the existing hold actionable, but the prescribed EDA
compatibility matrix is migration work rather than a routine release refresh.

## EDA, simulation, synthesis, and lint tools

| Program | Pin on audited `main` | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Yosys, ABC, and slang | `v0.68` / `38e001a6...`; ABC and slang follow its submodules | `v0.68` | [Yosys v0.68](https://github.com/YosysHQ/yosys/releases/tag/v0.68) | **current** |
| OpenROAD | `26Q3` / `a9147cf3...`; official OCI index `sha256:c34542dd...` | `26Q3` | [OpenROAD 26Q3](https://github.com/The-OpenROAD-Project/OpenROAD/tree/26Q3), [OCI tag](https://hub.docker.com/v2/repositories/openroad/ubuntu24.04/tags/26Q2-2580-ga9147cf3ae) | **current**: the source tag, OCI provenance, and pinned index resolve to the same commit. |
| Icarus Verilog and `vvp` | `v13_0` / `dfeee909...` | `v13_0` | [Icarus v13_0](https://github.com/steveicarus/iverilog/releases/tag/v13_0) | **current** |
| Verilator | `v5.046` / `24b2ac24...` | `v5.052` | [Verilator v5.052](https://github.com/verilator/verilator/releases/tag/v5.052) | **held**: 5.052 descends from compiler fix `a6f4dd03...`, but the compiler, Cocotb, FST, diagnostic, and coverage compatibility matrix must pass before the supported EDA version changes. Follow-up [#393](https://github.com/boldaxolotl/booley/issues/393). |
| sv2v | `v0.0.13` | `v0.0.13` | [sv2v v0.0.13](https://github.com/zachjs/sv2v/releases/tag/v0.0.13) | **current** |
| Verible | `v0.0-4163-g6cce8f19` | same | [Verible release](https://github.com/chipsalliance/verible/releases/tag/v0.0-4163-g6cce8f19) | **current** |
| AMD Vivado | accepted host version `2025.2` | `2026.1` | [Vivado 2026.1](https://www.amd.com/en/support/downloads/adaptive-socs-and-fpgas/development-tools/2026-1.html) | **held**: changing the commercial-tool contract requires licensed-host evidence for licensing, discovery, cache identity, wrappers, fixtures, and documentation. See [#155](https://github.com/boldaxolotl/booley/issues/155). |
| Edalize | `0.6.8` | `0.6.8` | [PyPI](https://pypi.org/project/edalize/) | **current** |
| FuseSoC | `2.4.6` | `2.4.6` | [FuseSoC v2.4.6](https://github.com/olofk/fusesoc/releases/tag/2.4.6) | **current** |
| cocotb / `cocotb-config` | `2.1.0` | `2.1.0` | [cocotb v2.1.0](https://github.com/cocotb/cocotb/releases/tag/v2.1.0) | **current** |
| cocotbext-axi | `0.1.28` | `0.1.28` | [PyPI](https://pypi.org/project/cocotbext-axi/) | **current** |
| cocotbext-uart | `0.1.4` | `0.1.4` | [PyPI](https://pypi.org/project/cocotbext-uart/) | **current** |
| NumPy | `2.5.2` | `2.5.3` | [PyPI](https://pypi.org/project/numpy/) | **routine update**: refresh the exact Session Runtime pin, runtime import check, tests, and supported-tool table together. |

Commercial simulators and EDA suites mentioned only in roadmap, parsing, or
incubation material are not invoked dependencies. Xcelium and VCS integrations
parse captured output but do not launch those tools. GHDL, Questa/ModelSim,
Design/Fusion Compiler, Genus, HAL, SpyGlass, Verdi, JasperGold, and Quartus are
documentation-only or unsupported.

## RISC-V tools

| Program | Installed pin | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| xPack RISC-V GCC | `15.2.0-1` | `15.2.0-1` | [xPack release](https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/tag/v15.2.0-1) | **current** |
| Spike / riscv-isa-sim | tested master snapshot `c09c0cce...` | formal release `v1.1.0`; the pin is a newer tested snapshot | [Spike v1.1.0](https://github.com/riscv-software-src/riscv-isa-sim/releases/tag/v1.1.0) | **current** on Booley's validated-snapshot lane: v1.1.0 does not build on Ubuntu 24.04, while the pin passed the RISC-V image and differential-flow gates. See [the channel decision](spike-release-channel.md) and [#157](https://github.com/boldaxolotl/booley/issues/157). |
| `srec_cat`, `dtc`, `pdftotext` | Ubuntu 24.04 archive packages | Ubuntu 24.04 archive versions at build time | [Ubuntu packages](https://packages.ubuntu.com/) | **unversioned**: apt resolves these within the selected image archive. |

The RISC-V ISA, debug, and psABI PDFs and HTML archives are reference data,
not executable tools. Their immutable checksums remain unchanged.

## Agent CLIs, runtimes, compilers, and images

| Program or image | Pin on audited `main` | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Claude Code | `2.1.259` | `2.1.263` | [Claude Code v2.1.263](https://github.com/anthropics/claude-code/releases/tag/v2.1.263) | **routine update**: compatible reliability fixes; refresh the npm manifest, lockfile, image assertions, and tests together. |
| Codex CLI | `0.153.1` | `0.153.4` | [Codex 0.153.4](https://github.com/openai/codex/releases/tag/rust-v0.153.4) | **routine update**: compatible model-picker and guidance hotfixes; refresh the npm manifest, lockfile, image assertions, and tests together. |
| Node.js | `24.20.0` | `24.20.0` on the supported 24.x LTS lane | [Node release index](https://nodejs.org/dist/index.json) | **current** |
| Python sidecars | `3.14.7` on Bookworm and Alpine 3.24; indexes `sha256:9ab8d9c8...` and `sha256:c6ead215...` | same tags and index digests | [slim-bookworm tag](https://hub.docker.com/v2/repositories/library/python/tags/3.14.7-slim-bookworm), [Alpine tag](https://hub.docker.com/v2/repositories/library/python/tags/3.14.7-alpine3.24) | **current** |
| Python 3.13 sidecar controls | immutable `3.13.15` Bookworm and Alpine 3.24 indexes used only as before-upgrade evidence | current tag digests differ | [slim-bookworm tag](https://hub.docker.com/v2/repositories/library/python/tags/3.13.15-slim-bookworm), [Alpine tag](https://hub.docker.com/v2/repositories/library/python/tags/3.13.15-alpine3.24) | **held**: the pins deliberately preserve the historical Python 3.13 control payload. |
| Rust/Cargo builder | `rust:1.98.0-slim-bookworm@sha256:1469a27c...` | Rust `1.98.1`; no official `1.98.1-slim-bookworm` image exists | [Rust 1.98.1](https://github.com/rust-lang/rust/releases/tag/1.98.1), [official image tags](https://hub.docker.com/v2/repositories/library/rust/tags) | **held**: retain 1.98.0 until the required official image exists for immutable pinning and validation. |
| Scheduled fuzz compiler | `nightly-2026-08-20` | nightly is not a stable release channel | [Rust toolchains](https://rust-lang.github.io/rustup/concepts/toolchains.html) | **held**: this is a tested fuzzing snapshot; move it only for a cargo-fuzz or LLVM requirement. |
| Docker CLI in reaper | `29.7.2-cli@sha256:3f474320...` | `29.8.0-cli@sha256:eccaacfe...` | [Docker 29.8.0](https://github.com/moby/moby/releases/tag/docker-v29.8.0), [official image](https://hub.docker.com/v2/repositories/library/docker/tags/29.8.0-cli) | **routine update**: refresh the CLI stage, exact digest assertions, and sidecar build evidence together. |
| Docker-in-Docker evidence service | `29.7.2-dind@sha256:3ef33f2e...` | `29.8.0-dind@sha256:5efed980...` | [official image](https://hub.docker.com/v2/repositories/library/docker/tags/29.8.0-dind) | **routine update**: refresh the isolated DinD evidence service with the CLI lane. |
| Ubuntu runtime parent | `24.04@sha256:33ceb719...` | same tag and index digest | [official image](https://hub.docker.com/v2/repositories/library/ubuntu/tags/24.04) | **current** |
| OpenROAD Ubuntu 24.04 runtime/development bases | `sha256:c34542dd...` / `sha256:1cfdeba8...` | same immutable 26Q3 publisher artifacts | [official runtime image](https://hub.docker.com/v2/repositories/openroad/ubuntu24.04/tags/26Q2-2580-ga9147cf3ae) | **current** |

Both agent CLI launchers obtain Linux/x64 executables from platform-specific
npm artifacts. Exact required dependencies make a missed artifact fail
`npm ci`.

## Development, build, test, and publication tools

| Tool | Installed pin or supported lane | Current stable production release | Source | Outcome |
| --- | --- | --- | --- | --- |
| Ruff | `0.16.6` | `0.16.6` | [Ruff v0.16.6](https://github.com/astral-sh/ruff/releases/tag/0.16.6) | **current** |
| pytest | CI `9.1.1`; project lane `>=8.0` | `9.1.1` | [PyPI](https://pypi.org/project/pytest/) | **current** |
| pytest-asyncio | CI `1.4.0`; project lane `>=0.23` | `1.4.0` | [PyPI](https://pypi.org/project/pytest-asyncio/) | **current** |
| pytest-cov | `7.1.0` | `7.1.0` | [PyPI](https://pypi.org/project/pytest-cov/) | **current** |
| pytest-xdist | supported lane `>=3.8` | `3.8.0` | [PyPI](https://pypi.org/project/pytest-xdist/) | **current** |
| pytest-timeout | supported lane `>=2.3` | `2.4.0` | [PyPI](https://pypi.org/project/pytest-timeout/) | **current** |
| coverage.py | resolved through pytest-cov | `7.16.0` | [PyPI](https://pypi.org/project/coverage/) | **unversioned**: no direct repository pin. |
| diff-cover | `10.5.1` | `10.5.1` | [PyPI](https://pypi.org/project/diff-cover/) | **current** |
| Pyright | `1.1.411` | `1.1.411` | [PyPI](https://pypi.org/project/pyright/) | **current** |
| mutmut | `3.7.0` | `3.7.0` | [PyPI](https://pypi.org/project/mutmut/) | **current** |
| PyYAML semantic checker | `6.0.2` | `6.0.3` | [PyPI](https://pypi.org/project/PyYAML/) | **routine update**: refresh the exact workflow pin and rerun release-semantic. |
| cargo-fuzz | `0.13.2` | `0.13.2` | [cargo-fuzz 0.13.2](https://github.com/rust-fuzz/cargo-fuzz/releases/tag/0.13.2) | **current** |
| actionlint | `1.7.12` plus exact SHA256 | `1.7.12` | [actionlint v1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12) | **current** |
| setuptools build backend | supported lane `>=84.0.0` | `84.0.0` | [PyPI](https://pypi.org/project/setuptools/) | **current** |
| PyPA build | workflow pin `1.6.0`; project lane `>=1.0` | `1.6.0` | [PyPI](https://pypi.org/project/build/) | **current** |
| Twine | `7.0.0` | `7.0.0` | [PyPI](https://pypi.org/project/twine/) | **current** |
| `softprops/action-gh-release` | `v3.0.3` / `efb35369...` | `v3.0.3` | [v3.0.3](https://github.com/softprops/action-gh-release/releases/tag/v3.0.3) | **current** |

The other immutable GitHub Actions pins resolve to their current stable
releases: checkout `v7.0.1`, setup-python `v7.0.0`, upload-artifact `v7.0.1`,
download-artifact `v8.0.1`, setup-buildx `v4.3.0`, login `v4.6.0`, build-push
`v7.3.0`, rust-cache `v2.9.2`, and `gh-action-pypi-publish` `v1.14.2`.
The pinned `dtolnay/rust-toolchain` commit follows the production `stable`
channel. Outcome: **current**.

## Host-provided and absent categories

Host Docker, Git, Bash, Cargo, GitHub CLI, Windows command tools, PowerShell,
editors, project hooks, user-configured credential commands, and host EDA
executables other than the exact Vivado acceptance contract have no repository
version pin. Outcome: **unversioned**.

Ubuntu apt installs compilers and utilities without individual versions. They
float only within the selected immutable image. Outcome: **unversioned**.

Repository search found no FFmpeg/`ffprobe`, codec suite, AWS/Azure/Google
Cloud CLI, `kubectl`, Helm, Terraform, or independently pinned service/cloud
CLI. No update is applicable for those absent categories.
