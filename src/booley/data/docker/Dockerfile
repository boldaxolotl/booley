# Booley sandbox image — agents and sim/synth run inside this container.
# Host worktree is bind-mounted at /work; no file sync needed.
#
# Build:
#   ./src/booley/data/docker/build.sh          (auto-builds wheel)
#   docker build -t booley-sandbox .           (wheel in dist/ required)

# ---------------------------------------------------------------------------
# bwave builder — throwaway stage whose only output is the native binary.
#
# Kept OUT of the final image deliberately: a Rust toolchain is ~1.6 GB and is
# unreachable at runtime anyway (the crate source is not shipped, and the
# binary is installed by the final stage). Building here rather than pulling
# the binary from the wheel is what keeps `build.sh` working on a host with no
# cargo — the base image can only be built from a source checkout
# (init_docker_image._build_docker_image hard-errors without a repo root), but
# that checkout is not guaranteed to have a Rust toolchain installed.
#
# Pinned to a *bookworm* builder on purpose: its glibc (2.36) is older than the
# final image's Ubuntu 24.04 (2.39), so the binary's symbol requirements
# (max GLIBC_2.34) resolve there. A newer builder base would link against
# symbols 24.04 does not have and fail at exec time, not at build time.
FROM rust:1.97.1-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3 AS bwave-builder
COPY crates/bwave/ /build/bwave/
RUN CARGO_TARGET_DIR=/build/target cargo build --release --locked \
        --manifest-path /build/bwave/Cargo.toml

FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

# Tolerate host clock skew during image build (win_test): on Windows hosts the
# Docker Desktop/WSL2 VM inherits the host clock; a skewed host (common on
# dual-boot RTC-vs-UTC setups) makes apt reject freshly-published Release
# files as "not valid yet" and the build dies in the first layer. Date
# validation adds nothing here — package integrity is still GPG-verified.
RUN printf 'Acquire::Check-Date "false";\n' > /etc/apt/apt.conf.d/99-tolerate-clock-skew

# System deps — Ubuntu 24.04 ships Python 3.12; add deadsnakes for 3.13
RUN echo ">>> Installing system dependencies and Python 3.13..." \
    && apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential make gcc g++ \
    software-properties-common \
    ripgrep unzip xz-utils jq \
    autoconf flex bison gperf libfl-dev help2man \
    locales libpixman-1-0 libudev1 \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3.13 python3.13-venv python3.13-dev \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.13 /usr/bin/python3 \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Worktrees are bind-mounted from the host with different ownership;
# git refuses to operate without this.  Wildcard covers /work and any
# path the .git worktree pointer resolves to (e.g. the main repo's
# .git/worktrees/ directory which lives outside the mount).
# Identity default is deliberately neutral (A-7): stealth mode's sanitizer
# keeps product/AI terms out of commit MESSAGES, and a "Booley Agent" author
# would defeat it in the metadata of every agent commit. Projects override via
# [agent.git] in booley.toml (applied per-worktree by worktree_create.sh).
RUN git config --system --add safe.directory '*' \
    && git config --system user.name "Dev" \
    && git config --system user.email "dev@localhost"

# Yosys + ABC + native slang frontend, built from source.
#
# Yosys 0.67 replaced the old `make config-gcc` Makefile flow with CMake
# (>=3.28; Ubuntu 24.04 ships 3.28.3). The SystemVerilog `read_slang` frontend
# (povik/sv-elab on top of MikePopoloski/slang) is a git submodule and is
# ENABLED BY DEFAULT — there is no enable flag; slang is built unless
# -DYOSYS_WITHOUT_SLANG=ON is passed. Its C++ deps (fmt/cxxopts/boost_regex)
# are vendored as Yosys submodules, so no extra apt packages beyond the base
# image's flex/bison/libfl-dev (line ~23) plus cmake/gawk here. Submodules are
# fetched full (NOT --shallow-submodules): sv-elab/slang pin non-tip commits a
# shallow submodule fetch can miss.
ARG YOSYS_VERSION=v0.67
ARG YOSYS_REF=2d1509d1bcb8df0723f6790057e3b1d21c876683
RUN echo ">>> Building Yosys ${YOSYS_VERSION} (${YOSYS_REF}) + slang from source (this takes a while)..." \
    && apt-get update && apt-get install -y --no-install-recommends \
        cmake gawk pkg-config tcl-dev libreadline-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git init /tmp/yosys \
    && cd /tmp/yosys \
    && git remote add origin https://github.com/YosysHQ/yosys.git \
    && git fetch --depth 1 origin "${YOSYS_REF}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${YOSYS_REF}" \
    && git submodule update --init --recursive \
    && cmake -B build -S . \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER=gcc \
        -DCMAKE_CXX_COMPILER=g++ \
    && cmake --build build --parallel "$(nproc)" \
    && cmake --install build --strip \
    && yosys-abc -c "quit" \
    && yosys -p 'help read_slang' >/dev/null \
    && rm -rf /tmp/yosys

# OpenSTA for post-synthesis timing reports from the simple Yosys backend.
ARG CUDD_VERSION=3.0.0
ARG CUDD_REF=f54f533303640afd5dbe47a05ebeabb3066f2a25
ARG OPENSTA_REF=4249ab7b98246180db361b340b43ccfe2053054a
RUN echo ">>> Building OpenSTA ${OPENSTA_REF} from source..." \
    && apt-get update && apt-get install -y --no-install-recommends \
        cmake automake file libtool swig libeigen3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git init /tmp/cudd \
    && cd /tmp/cudd \
    && git remote add origin https://github.com/cuddorg/cudd.git \
    && git fetch --depth 1 origin "${CUDD_REF}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${CUDD_REF}" \
    && autoreconf -fi \
    && ./configure --prefix=/opt/cudd \
    && make -j$(nproc) \
    && make install \
    && git init /tmp/OpenSTA \
    && cd /tmp/OpenSTA \
    && git remote add origin https://github.com/parallaxsw/OpenSTA.git \
    && git fetch --depth 1 origin "${OPENSTA_REF}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${OPENSTA_REF}" \
    && cmake -S /tmp/OpenSTA -B /tmp/OpenSTA/build \
        -DCMAKE_BUILD_TYPE=RELEASE \
        -DCUDD_DIR=/opt/cudd \
    && cmake --build /tmp/OpenSTA/build --parallel $(nproc) \
    && install -m 755 /tmp/OpenSTA/build/sta /usr/local/bin/sta \
    && sta -version \
    && rm -rf /tmp/cudd /tmp/OpenSTA

# OpenROAD (pinned prebuilt .deb) — default timing engine: placement +
# buffering/sizing + estimated wire RC. The ubuntu-22.04 deb installs on
# 24.04 (Qt5 t64 Provides old names; deadsnakes ships libpython3.10).
# Newer 24.04-native debs sit behind a registration wall, so pin the last
# public asset.
ARG OPENROAD_RELEASE_TAG=2024-12-14
ARG OPENROAD_VERSION=2.0-17598-ga008522d8
ARG OPENROAD_SHA256=40ed178396b0276a5d5dfbbe695c9de9aac9088157a6655be02b39a0cef07207
RUN echo ">>> Installing OpenROAD ${OPENROAD_VERSION} (prebuilt deb)..." \
    && curl --proto '=https' --tlsv1.2 -fsSL "https://github.com/Precision-Innovations/OpenROAD/releases/download/${OPENROAD_RELEASE_TAG}/openroad_${OPENROAD_VERSION}_amd64-ubuntu-22.04.deb" -o /tmp/openroad.deb \
    && echo "${OPENROAD_SHA256}  /tmp/openroad.deb" | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/openroad.deb \
    && rm -f /tmp/openroad.deb && rm -rf /var/lib/apt/lists/* \
    && openroad -version

# Icarus Verilog is built from source because Ubuntu packages lag releases.
ARG ICARUS_VERSION=v13_0
ARG ICARUS_REF=dfeee909ed9f20b4870dd93423156c0170c0e1ff
RUN echo ">>> Building Icarus Verilog ${ICARUS_VERSION} (${ICARUS_REF}) from source..." \
    && apt-get update && apt-get install -y --no-install-recommends \
        libreadline-dev zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git init /tmp/iverilog \
    && cd /tmp/iverilog \
    && git remote add origin https://github.com/steveicarus/iverilog.git \
    && git fetch --depth 1 origin "${ICARUS_REF}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${ICARUS_REF}" \
    && sh autoconf.sh \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/iverilog

# Verilator is built from source so host installations do not affect results.
# v5.048 currently trips an internal error elaborating some downstream designs (force + unpacked arrays).
ARG VERILATOR_VERSION=v5.046
ARG VERILATOR_REF=24b2ac24c721fdad89bba75a492e02c6aa63f32e
RUN echo ">>> Building Verilator ${VERILATOR_VERSION} (${VERILATOR_REF}) from source (this takes a while)..." \
    && git init /tmp/verilator \
    && cd /tmp/verilator \
    && git remote add origin https://github.com/verilator/verilator.git \
    && git fetch --depth 1 origin "${VERILATOR_REF}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${VERILATOR_REF}" \
    && autoconf && ./configure --prefix=/usr/local \
    && make -j$(nproc) && make install \
    && rm -rf /tmp/verilator

# sv2v (pinned release binary)
ARG SV2V_VERSION=v0.0.12
ARG SV2V_SHA256=ff8c9eea5bc029b372fb4953427625cddb7cf7e58c1240623ac9f260818d5a00
RUN echo ">>> Installing sv2v ${SV2V_VERSION}..." \
    && curl --proto '=https' --tlsv1.2 -fsSL "https://github.com/zachjs/sv2v/releases/download/${SV2V_VERSION}/sv2v-Linux.zip" \
    -o /tmp/sv2v.zip \
    && echo "${SV2V_SHA256}  /tmp/sv2v.zip" | sha256sum -c - \
    && unzip -o /tmp/sv2v.zip -d /tmp/sv2v \
    && cp /tmp/sv2v/sv2v-Linux/sv2v /usr/local/bin/sv2v \
    && chmod +x /usr/local/bin/sv2v && rm -rf /tmp/sv2v.zip /tmp/sv2v

# Verible (pinned prebuilt static binary — the sv2v pattern). Sandbox-only
# lint EDA tool for .core lint Targets with flow_options.tool: verible (ADR 0033).
ARG VERIBLE_VERSION=v0.0-4080-ga0a8d8eb
ARG VERIBLE_SHA256=f75daa70f29dbe9624ffee3738408341cfdadbdaf7e5d714a5bcceb9223953e6
RUN echo ">>> Installing Verible ${VERIBLE_VERSION}..." \
    && curl --proto '=https' --tlsv1.2 -fsSL "https://github.com/chipsalliance/verible/releases/download/${VERIBLE_VERSION}/verible-${VERIBLE_VERSION}-linux-static-x86_64.tar.gz" \
    -o /tmp/verible.tar.gz \
    && echo "${VERIBLE_SHA256}  /tmp/verible.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/verible.tar.gz -C /tmp \
    && cp /tmp/verible-${VERIBLE_VERSION}/bin/* /usr/local/bin/ \
    && rm -rf /tmp/verible.tar.gz /tmp/verible-${VERIBLE_VERSION} \
    && verible-verilog-lint --version

# Node.js 22 LTS + agent CLIs. Download exact publisher artifacts and verify
# them before execution; neither a mutable setup script nor semver ranges are
# allowed in the release image.
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
COPY src/booley/data/docker/agent-clis-package.json /opt/agent-clis/package.json
COPY src/booley/data/docker/agent-clis-package-lock.json /opt/agent-clis/package-lock.json
RUN echo ">>> Installing Node.js ${NODE_VERSION} and exact agent CLIs..." \
    && curl --proto '=https' --tlsv1.2 -fsSL \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm -f /tmp/node.tar.xz \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && npm ci --prefix /opt/agent-clis --omit=dev --no-audit --no-fund \
    && ln -s /opt/agent-clis/node_modules/.bin/claude /usr/local/bin/claude \
    && ln -s /opt/agent-clis/node_modules/.bin/codex /usr/local/bin/codex \
    && test "$(claude --version | awk '{print $1}')" = "2.1.234" \
    && test "$(codex --version | awk '{print $2}')" = "0.147.0" \
    && command -v bwrap \
    && rm -rf /var/lib/apt/lists/*

# The container's network namespace is not the whole agent boundary: both
# clients can expose provider-hosted web MCP tools whose requests never originate
# in Docker. Enforce the offline contract in each client's system policy so
# user/project config and full-access modes cannot turn those MCP tools back on.
COPY src/booley/data/docker/codex-requirements.toml /etc/codex/requirements.toml
COPY src/booley/data/docker/claude-managed-settings.json /etc/claude-code/managed-settings.json

# Non-root user for agent execution.
# Pin agent to uid/gid 1000 so it matches the typical first host user on native
# Linux; the base ubuntu:24.04 image already occupies 1000 with its default
# "ubuntu" user, so remove that first. Without this, bind-mounted project files
# (owned by host uid 1000) are unwritable by the container and the MCP server
# fails to create its .interactive_logs dir.
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd -g 1000 agent \
    && useradd -m -s /bin/bash -u 1000 -g 1000 agent

# Install the candidate's declared dependencies behind a cache boundary that
# changes only when pyproject.toml changes. The exporter preserves every PEP
# 508 string verbatim, including environment markers, and pip remains the one
# resolver for both this stable layer and ordinary wheel installation.
# Dependencies go system-wide so the booley-pip-local named volume cannot mask
# them at runtime.
#
# claude-agent-sdk ships a private, full-size Claude CLI even though this
# image already installs the normal npm CLI above.  Booley resolves that
# system CLI and passes its path to ClaudeAgentOptions, so retaining the SDK
# copy wastes ~324 MB and is not a fallback in this runtime.  Remove it in
# the SAME layer that pip creates it; a later-layer deletion would hide the
# file from the merged filesystem without reclaiming any image storage.
COPY pyproject.toml /tmp/booley-build/pyproject.toml
COPY src/booley/data/docker/export_project_dependencies.py /tmp/booley-build/export_project_dependencies.py
RUN python -m ensurepip --default-pip \
    && python /tmp/booley-build/export_project_dependencies.py \
        /tmp/booley-build/pyproject.toml \
        /tmp/booley-build/project-dependencies.txt \
    && python -m pip install --break-system-packages --no-cache-dir --ignore-installed \
        --requirement /tmp/booley-build/project-dependencies.txt \
    && rm -f /tmp/booley-build/project-dependencies.txt \
    && CLAUDE_SDK_BUNDLED_CLI="$(python -c 'from pathlib import Path; import claude_agent_sdk; print(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')" \
    && test -x "$(command -v claude)" \
    && test -x "$CLAUDE_SDK_BUNDLED_CLI" \
    && rm -f "$CLAUDE_SDK_BUNDLED_CLI" \
    && test ! -e "$CLAUDE_SDK_BUNDLED_CLI"

# EDA invocation/registry layer (ADR 0019 + 0022) — pinned EXACTLY to the
# flow-API-stable pair. These also arrive transitively via the wheel's
# dependencies; pinning them explicitly here makes the contract visible in
# the image and guards against a future loosening of the wheel constraint.
# Edalize only *invokes* the EDA tools built above; it does not manage their
# versions (ADR 0019 decision).
RUN python -m pip install --break-system-packages --no-cache-dir \
    "edalize==0.6.8" "fusesoc==2.4.6"

# Cocotb verification layer (ADR 0034 decision 10) — cocotb 2.x plus the
# small curated, pinned Python set a testbench may import (Booley Flows have no
# network, so anything beyond this set is vendored in the project tree).
# cocotbext-spi is ABSENT deliberately: no release imports against cocotb 2.x
# (0.5.0 still uses the removed cocotb.clock.BaseClock).
RUN python -m pip install --break-system-packages --no-cache-dir \
    "cocotb==2.0.1" "numpy==2.5.1" "cocotbext-axi==0.1.28" "cocotbext-uart==0.1.4"

# Build-time sanity (D2): the image fails to build rather than failing at
# first sim — cocotb-config resolves, the curated set imports, and the
# Icarus VPI library the run-half loads (`vvp -m cocotbvpi_icarus`) exists.
# (vvp resolves `-m libcocotbvpi_icarus` by appending the platform suffix,
# so the shipped file is the extensionless lib-name-path + ".vpl".)
RUN cocotb-config --version \
    && python3 -c "import cocotb, cocotbext.axi, cocotbext.uart, numpy" \
    && test -e "$(cocotb-config --lib-name-path vpi icarus).vpl"

# Edalize patch (ADR 0033): install the Booley-authored flow-API Verible lint
# EDA-tool node into the pinned Edalize that the `fusesoc` subprocess imports.
# edalize 0.6.8's lint flow has no Verible node (upstream lists it as a
# comment); its Generic flow needs no FLOW_DEFINED_TOOL_OPTIONS entry, so only
# Edalize's tools/verible.py is patched in. DELETE THIS LAYER once the upstream
# Edalize PR (tools/verible.py) is merged and the pin above advances past it.
COPY src/booley/data/edalize/verible.py /tmp/booley-build/verible.py
RUN python -c 'import pathlib, shutil, edalize.tools; src = pathlib.Path("/tmp/booley-build/verible.py"); dst = pathlib.Path(list(edalize.tools.__path__)[0]) / "verible.py"; shutil.copyfile(src, dst); print(f"patched {dst}")' \
    && rm -f /tmp/booley-build/verible.py

# Candidate seam. The wheel arrives only after invariant Python/EDA layers and
# is installed without dependency resolution because the unchanged PEP 508
# requirements were already resolved above. Validate its version and complete
# package inventory against the installed distribution before discarding it.
# Wheel filename uses the PEP 427-normalized distribution name (booley-rtl -> booley_rtl).
COPY .github/scripts/validate_installed_artifact.py /tmp/booley-build/validate_installed_artifact.py
COPY dist/booley_rtl-*.whl /tmp/booley-build/
RUN WHEEL="$(find /tmp/booley-build -maxdepth 1 -name 'booley_rtl-*.whl' -print -quit)" \
    && test -n "$WHEEL" \
    && python -m pip install --break-system-packages --no-cache-dir --ignore-installed \
        --no-deps "$WHEEL" \
    && cd /tmp \
    && BOOLEY_CONTAINER=1 python /tmp/booley-build/validate_installed_artifact.py \
        --inventory /tmp/booley-installed-files.txt \
        --forbid-root /work \
        --wheel "$WHEEL" \
    && rm -f "$WHEEL" /tmp/booley-installed-files.txt

# Candidate-aware runtime sanity stays after installation: the normal npm CLI
# must win over the SDK's removed private copy, and invariant integrations must
# remain importable alongside the installed application.
RUN python -c 'from booley.runtime._claude_backend import ClaudeSDKBackend; backend = ClaudeSDKBackend(); backend._resolve_cli_shim_once(); assert backend._cli_path and "_bundled" not in backend._cli_path' \
    && python -c 'import booley, cocotb, edalize.tools.verible, fusesoc; print(booley.__file__)'

USER agent

# Pre-create pip user-install dirs so named volumes inherit agent ownership
RUN mkdir -p /home/agent/.local/lib/python3.13/site-packages /home/agent/.local/bin
ENV PATH="/home/agent/.local/bin:${PATH}"
ENV PYTHONUSERBASE="/home/agent/.local"

# Session Runtime detection (ADR 0028): marks every process in this image — including
# bare `docker exec` sessions, which devcontainer remoteEnv cannot reach —
# as running inside the Booley Session Runtime. /.dockerenv is the fallback
# for containers from images built before this was added.
ENV BOOLEY_CONTAINER=1

# Claude Code kills an MCP tool call at 60s by default (measured on 2.1.205;
# ADR 0027 amendment 2026-07-09). 2h matches the Codex tool_timeout_sec the
# registrar writes. NOTE: the effective ceiling is still ~300s — the client's
# HTTP layer drops a response whose headers haven't arrived by then, and the
# booley server (json_response=True) sends headers only on completion — so
# server-side waits are capped at 270s; this ENV removes the 60s MCP-level
# kill underneath that. Image-level ENV so it reaches every entry path —
# tabs, docker exec, the Ticket-Mode runner — even when a stale devcontainer
# spec predates it.
ENV MCP_TOOL_TIMEOUT=7200000

# bwave — VCD waveform parser for RTL debug. Built by the `bwave-builder`
# stage above and copied in as a bare binary, so containers with
# --network none can use it without downloading crates and without carrying
# the toolchain that produced it.
COPY --from=bwave-builder /build/target/release/bwave /tmp/bwave-bin

# The native binary is installed OFF PATH, in two places that are one inode:
#
#   1. the installed package's data/bin/ compatibility path, injected by this
#      image after the platform-neutral wheel is installed;
#   2. /usr/local/libexec/booley/bwave — a static path that does not move when
#      `booley` is imported from somewhere else. A bind-mounted source checkout
#      (Booley's own devcontainer) shadows the installed package, which drags
#      package_data_dir() into the mounted tree, where bin/ is empty — the
#      static path is what keeps the binary resolvable there. BOOLEY_BWAVE_BIN
#      names it for booley.runtime.paths.native_bwave_binary().
#
# Off PATH is the point: the only `bwave` on PATH must be the wrapper below.
# A native binary earlier on PATH shadows it, and a human typing `bwave gui`
# then reaches the Rust binary and gets "unrecognized subcommand" while the
# MCP tool — which calls the wrapper directly — works. (This bit historically:
# ~/.cargo/bin/bwave sat ahead of /usr/local/bin. That toolchain is gone now,
# but the placement rule is what keeps it gone.)
USER root
RUN BWAVE_BIN_DIR="$(python3 -c 'from booley.runtime.paths import package_data_dir; print(package_data_dir())')/bin" \
    && mkdir -p "$BWAVE_BIN_DIR" /usr/local/libexec/booley \
    && install -m 755 /tmp/bwave-bin "$BWAVE_BIN_DIR/bwave" \
    && ln -f "$BWAVE_BIN_DIR/bwave" /usr/local/libexec/booley/bwave \
    && rm -f /tmp/bwave-bin \
    && /usr/local/libexec/booley/bwave --version
ENV BOOLEY_BWAVE_BIN=/usr/local/libexec/booley/bwave

# bwave wrapper — the ONLY `bwave` on PATH; delegates to the Python EDA tool, which
# resolves the native binary through the paths above. Lives in /usr/local/bin
# (not /home/agent/.local/bin) so the booley-pip-local named volume can't
# shadow it at runtime.
RUN echo '#!/bin/bash\nexec python3 -m booley.bwave.cli "$@"' \
    > /usr/local/bin/bwave && chmod +x /usr/local/bin/bwave

# Built-in host-provisioned Vivado policy. The host release root is mounted
# read-only at the fixed target; only this Booley-owned launcher controls the
# compatibility preload and it invokes Vivado directly (never settings64.sh).
COPY src/booley/data/docker/vivado-wrapper /usr/local/bin/vivado
RUN chmod 755 /usr/local/bin/vivado
USER agent

# Pre-create writable dirs for agent CLIs (bind-mounting individual files
# into a non-existent dir would create it as root, breaking writes)
RUN mkdir -p /home/agent/.codex /home/agent/.claude

# Runtime provenance for `booley_status`. Keep this at the end so a new build
# timestamp changes only image metadata, not the expensive EDA-tool layers.
ARG BOOLEY_VERSION=unknown
ARG BOOLEY_SOURCE_REVISION=unknown
ARG BOOLEY_SOURCE_UPDATED_AT=unknown
ARG BOOLEY_IMAGE_BUILT_AT=unknown
ENV BOOLEY_VERSION=${BOOLEY_VERSION} \
    BOOLEY_SOURCE_REVISION=${BOOLEY_SOURCE_REVISION} \
    BOOLEY_SOURCE_UPDATED_AT=${BOOLEY_SOURCE_UPDATED_AT} \
    BOOLEY_IMAGE_BUILT_AT=${BOOLEY_IMAGE_BUILT_AT}
LABEL org.opencontainers.image.version=${BOOLEY_VERSION} \
      org.opencontainers.image.revision=${BOOLEY_SOURCE_REVISION} \
      org.opencontainers.image.created=${BOOLEY_IMAGE_BUILT_AT} \
      org.opencontainers.image.source=https://github.com/boldaxolotl/booley

WORKDIR /work
