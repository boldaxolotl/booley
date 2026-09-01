# Booley candidate image — thin application layer over the published runtime base.
# BuildKit maps this symbolic name to either an immutable GHCR digest or a
# locally built compatibility base through --build-context.
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
FROM rust:1.98.0-slim-bookworm@sha256:1469a27c125cb5a3aebfa4f4e4665d935b02fb72cc093b2c974b3d740e43f157 AS bwave-builder
COPY crates/bwave/ /build/bwave/
RUN CARGO_TARGET_DIR=/build/target cargo build --release --locked \
        --manifest-path /build/bwave/Cargo.toml

FROM booley-runtime-base
USER root

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
    && python -m pip check \
    && cd /tmp \
    && BOOLEY_CONTAINER=1 python /tmp/booley-build/validate_installed_artifact.py \
        --inventory /tmp/booley-installed-files.txt \
        --forbid-root /work \
        --wheel "$WHEEL" \
    && rm -f "$WHEEL" /tmp/booley-installed-files.txt

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

# Candidate-aware runtime sanity stays after installation: the SDK owns CLI
# discovery, its duplicate bundle remains absent, and the native system CLI
# plus invariant integrations remain available alongside the application.
RUN test -x "$(command -v claude)" \
    && test "$(claude --version | awk '{print $1}')" = "2.1.252" \
    && python -c 'import booley, cocotb, edalize.tools.verible, fusesoc; print(booley.__file__)'


# Runtime provenance for booley_status. Candidate timestamps cannot invalidate
# or obscure the stable base's independently labeled compatibility contract.
ARG BOOLEY_VERSION=unknown
ARG BOOLEY_SOURCE_REVISION=unknown
ARG BOOLEY_SOURCE_UPDATED_AT=unknown
ARG BOOLEY_IMAGE_BUILT_AT=unknown
ARG BOOLEY_RUNTIME_BASE_IMAGE=unknown
ARG BOOLEY_PAYLOAD_FINGERPRINT=unknown
ENV BOOLEY_VERSION=${BOOLEY_VERSION} \
    BOOLEY_SOURCE_REVISION=${BOOLEY_SOURCE_REVISION} \
    BOOLEY_SOURCE_UPDATED_AT=${BOOLEY_SOURCE_UPDATED_AT} \
    BOOLEY_IMAGE_BUILT_AT=${BOOLEY_IMAGE_BUILT_AT} \
    BOOLEY_PAYLOAD_FINGERPRINT=${BOOLEY_PAYLOAD_FINGERPRINT}
LABEL org.opencontainers.image.version=${BOOLEY_VERSION} \
      org.opencontainers.image.revision=${BOOLEY_SOURCE_REVISION} \
      org.opencontainers.image.created=${BOOLEY_IMAGE_BUILT_AT} \
      org.opencontainers.image.source=https://github.com/boldaxolotl/booley \
      io.booley.runtime-base.image=${BOOLEY_RUNTIME_BASE_IMAGE} \
      io.booley.provenance.schema=1 \
      io.booley.payload.fingerprint=${BOOLEY_PAYLOAD_FINGERPRINT} \
      booley.build-fingerprint=${BOOLEY_PAYLOAD_FINGERPRINT}

USER agent
WORKDIR /work
