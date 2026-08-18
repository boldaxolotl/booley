# Read-only mounted Vivado proof of concept

This fixture proves that an existing host Vivado installation can supply
read-only files while the complete Booley FPGA Flow executes inside the
hardened Session Runtime. It deliberately uses a tiny out-of-context Artix-7
design so the check exercises project generation, synthesis, placement,
routing, report parsing, and the final Booley verdict without board I/O.

The expected host path is one Xilinx **release root**, containing both
`Vivado/` and `tps/`. Mounting only `Vivado/` starts the launcher but omits
release-level runtime files.

Build the small compatibility image:

```console
docker build \
  -f tests/fixtures/vivado_mount_poc/Dockerfile \
  -t booley-sandbox-vivado-poc \
  tests/fixtures/vivado_mount_poc
```

Run the opt-in end-to-end check:

```console
BOOLEY_VIVADO_ROOT=/opt/Xilinx/2025.2 \
  pytest -q tests/docker/test_vivado_mount_e2e.py
```

The compatibility image adds `en_US.UTF-8` and `libpixman-1-0`. The test also
preloads the container's own `libudev.so.1`; without that preload Vivado can
load libudev late during licensing/telemetry discovery and abort inside
`udev_enumerate_scan_devices()` on a newer shared host kernel.

Security properties exercised by the test:

- the Xilinx release is a `readonly` bind;
- Vivado and `make` run as the non-root `agent` user;
- the container has no network, no Linux capabilities, and
  `no-new-privileges`;
- only the disposable project copy is writable;
- Host MCP is not involved.

This is an opt-in PoC, not the final user-facing mount configuration. A
production design must keep host source-path approval outside the
agent-writable workspace and expose only approved named tools.
