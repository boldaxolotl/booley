### Project Setup contract

Used by the mission's `setup` area; `targets-doctor` and `lint-synth` check the resulting Targets. One delegated Setup agent performs Project Initialization and the complete Project Setup sequence. This file supplies every decision, and the operator's hand-off grants the corresponding authority. The agent is not asked to seek or manufacture a second approval. `SETUP-PLAN.md` is a record of what it did, not a new runtime authorization gate; save it under `evidence/setup/`.

The Setup-agent intent is:

> Set up this fresh pinned Taxi checkout as a Booley Project. All design decisions in this prompt are approved; complete planning and execution without requesting further approval. Do not modify any existing Taxi RTL, testbench, manifest, symlink, tracked file, or committed history. Create a Project-derived image from the pinned dependency set listed below. Configure `sim_mac_10g`, `lint_mac_10g`, and `synth_mac_10g` for the specified 64-bit PTP/PFC/statistics configuration, using Verilator Cocotb with FST tracing, Verilator lint, and logical Yosys with slang respectively, and add `lint_mac_10g_verible` as described below. Resolve source paths without depending on `src/eth/lib/taxi` being a functional host symlink. Register all seven upstream Cocotb tests, explicitly disable Stealth Mode, and finish only after plain and deep Doctor are warning-free. Retain the Setup Plan, configuration diff, image identity, Target inventory, Doctor reports, and every deviation.

The agent owns the exact `.core`, requirements-file, and configuration spelling. The following outcomes are mandatory:

- `sim_mac_10g` uses the upstream `test_taxi_eth_mac_10g.sv` wrapper and `test_taxi_eth_mac_10g.py` Cocotb module, the full canonicalized source closure from `taxi_eth_mac_10g.f`, Verilator, all seven test-function registrations, and a `trace.fst` for traced runs. Use a VCD trace recipe, which Booley converts to FST; Cocotb Targets reject `--trace-fst`.
- `lint_mac_10g` drives Verilator lint with its default warning set over the relevant SystemVerilog source set, with a Project-owned flat-port wrapper as its top, and is clean.
- `lint_mac_10g_verible` drives Verible over the same source set. Verible cannot parse the pinned `taxi_axis_xgmii_rx_64.sv`, so this Target's expected outcome is that syntax error. It is not selected for Doctor checks.
- `synth_mac_10g` drives logical Yosys with the slang frontend. `taxi_eth_mac_10g` has interface ports, so the top is a Project-owned flat-port wrapper that instantiates it. Where slang rejects a Taxi source, a Project-owned synthesis-only copy with the smallest equivalent change may replace it in the synthesis filesets; record each copy's diff as a deviation. It does not claim STA or tape-out significance.
- All five Targets use unambiguous identities and carry the agreed fixed parameter values where applicable. All but `lint_mac_10g_verible` are selected for the appropriate deep-Doctor checks.
- `[stealth] enabled = false` is explicit; omission is not equivalent.
- The Project-derived image contains the complete pinned Taxi test dependency set: pytest 8.3.4, pytest-xdist 3.6.1, pytest-split 0.10.0, cocotb 2.1.0, cocotb-bus 0.3.0, cocotb-test 0.2.6, cocotbext-axi 0.1.28, cocotbext-eth 0.1.28, cocotbext-i2c 0.1.2, cocotbext-pcie 0.2.16, cocotbext-uart 0.1.4, and scapy 2.6.1. cocotb 2.1.0 is an approved deviation from upstream `tox.ini`, whose 2.0.1 does not install on the Sandbox's Python 3.14. Runtime network installation is forbidden. The real testbench import path is verified inside the Sandbox.
- CLI and MCP Target discovery agree on identity, selectors, EDA programs, and toplevels. Resolved parameters and source inputs are verified through the CLI detail view; MCP has no resolved view.
- Project Setup uses only published setup documentation, packaged skills, cheat sheets, CLI/MCP help, and ordinary Project inspection until an observation requiring source verification has been captured.

### Physical synthesis requirements

The Setup agent must also satisfy these requirements:

> Also configure persistent `synth_mac_10g_physical` for the same complete `taxi_eth_mac_10g` source closure and every approved parameter, using slang/Yosys followed by OpenROAD physical synthesis, explicit balanced PPA profile and flattening. It uses the same flat-port wrapper and synthesis-only copies as `synth_mac_10g`. Author and select the provided five-clock SDC in Project-owned configuration without editing existing Taxi files. Keep `synth_mac_10g` logical. Include both synthesis Targets in CLI/MCP discovery and appropriate deep Doctor checks. Preserve physical tool/library/recipe identities, SDC hash and clock/path coverage. Do not replace missing physical timing evidence with the logical frequency estimate, alter clock periods, disable enabled hardware, add timing waivers or replace the provided zero external-delay fixture with invented board I/O constraints.

### Representative hardware and test configuration

Use only the complete `taxi_eth_mac_10g`, instantiated by the flat-port wrapper where a Target needs one, with this fixed configuration:

```yaml
DATA_W: 64
TX_GBX_IF_EN: 0
RX_GBX_IF_EN: 0
GBX_CNT: 1
DIC_EN: 1
PTP_TS_EN: 1
PTP_TD_EN: 1
PTP_TS_FMT_TOD: 1
PTP_TS_FNS_W: 16
PTP_TS_W: 96
PTP_TD_SDI_PIPELINE: 2
TX_TAG_W: 16
PFC_EN: 1
PAUSE_EN: 1
STAT_EN: 1
STAT_TX_LEVEL: 2
STAT_RX_LEVEL: 2
STAT_ID_BASE: 0
STAT_UPDATE_PERIOD: 1024
STAT_STR_EN: 1
STAT_PREFIX_STR: MAC
```

Gearbox mode is explicitly disabled. This follows the pinned pytest driver rather than the Makefile default and is the representative standalone-MAC configuration. The full upstream module contains seven Cocotb functions: RX, TX, TX alignment, TX underrun, TX user error, LFC, and PFC. The RX and TX functions retain their IFG 12 and IFG 0 parameterizations.
