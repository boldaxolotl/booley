---
title: Documentation
document: User guide
description: The Booley user guide - setup, day-to-day use, Goal Mode, Flows, configuration, the Sandbox, supported EDA tools, Doctor, and troubleshooting.
---

# Documentation

The Booley user guide. The first time, read **Get started** in order.

| Section | Page | What it covers |
|---|---|---|
| **Get started** | [Features](https://boldaxolotl.github.io/booley/docs/features/) | Everything Booley does, and why. |
|  | [Setup](https://boldaxolotl.github.io/booley/docs/setup/) | Connect an installed Booley to your RTL project. |
|  | [Usage](https://boldaxolotl.github.io/booley/docs/usage/) | Day-to-day work with the Developer Agent and waveforms. |
|  | [Goal Mode](https://boldaxolotl.github.io/booley/docs/goal-mode/) | Evidence-backed Goals, Goalsets, and review. |
| **Flows** | [Running Flows](https://boldaxolotl.github.io/booley/docs/flow-reference/) | What every Flow shares, from choosing Targets to reading results. |
|  | [Simulation](https://boldaxolotl.github.io/booley/docs/flow-sim/) | `sim` tests, Test Runs, seeds, verdicts, and `[flows.sim]`. |
|  | [Coverage](https://boldaxolotl.github.io/booley/docs/coverage/) | Collect, gate, and analyze Verilator coverage. |
|  | [Lint](https://boldaxolotl.github.io/booley/docs/flow-lint/) | Verilator and Verible lint Targets. |
|  | [ASIC synthesis](https://boldaxolotl.github.io/booley/docs/flow-synth/) | Fast Yosys and OpenROAD area and timing estimates. |
|  | [FPGA implementation](https://boldaxolotl.github.io/booley/docs/flow-fpga/) | Vivado implementation, PPA profiles, and utilization. |
| **Reference** | [Config](https://boldaxolotl.github.io/booley/docs/config/) | Project-wide `booley.toml` knobs, and where the rest live. |
|  | [Targets and tests](https://boldaxolotl.github.io/booley/docs/targets/) | `.core` files, `tests.toml`, and Cocotb Targets. |
|  | [Sandbox](https://boldaxolotl.github.io/booley/docs/sandbox/) | Container policy, egress, custom images, and commercial EDA tools. |
|  | [Stealth Mode](https://boldaxolotl.github.io/booley/docs/stealth/) | Keep Booley out of your Git history. |
|  | [CLI reference](https://boldaxolotl.github.io/booley/docs/cli-reference/) | Every command, Flow, and Specialist. |
|  | [Supported EDA tools](https://boldaxolotl.github.io/booley/docs/supported-eda-tools/) | Which tools each Flow drives, and where they come from. |
| **Help** | [Doctor](https://boldaxolotl.github.io/booley/docs/doctor/) | Health checks for Booley's build and execution machinery. |
|  | [Troubleshooting](https://boldaxolotl.github.io/booley/docs/troubleshooting/) | Symptoms, causes, and fixes. |
|  | [Windows](https://boldaxolotl.github.io/booley/docs/windows/) | Windows-only first-run traps. |

These pages track the `main` branch, so they can describe changes newer than
your installed release; the [changelog](https://github.com/boldaxolotl/booley/blob/main/CHANGELOG.md)
says what each release contains. The source is [`docs/user/`](https://github.com/boldaxolotl/booley/tree/main/docs/user) on GitHub.
