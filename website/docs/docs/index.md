---
title: Documentation
document: User guide
description: The Booley user guide - setup, day-to-day use, Goal Mode, Flows, configuration, the Sandbox, supported EDA tools, Doctor, and troubleshooting.
---

# Documentation

The Booley user guide. The first time, read **Get started** in order.

**Get started**

- [Features](https://boldaxolotl.github.io/booley/docs/features/): everything Booley does, and why.
- [Setup](https://boldaxolotl.github.io/booley/docs/setup/): connect an installed Booley to your RTL project.
- [Usage](https://boldaxolotl.github.io/booley/docs/usage/): day-to-day work with the Developer Agent and waveforms.
- [Goal Mode](https://boldaxolotl.github.io/booley/docs/goal-mode/): evidence-backed Goals, Goalsets, and review.

**Flows**

- [Running Flows](https://boldaxolotl.github.io/booley/docs/flow-reference/): what every Flow shares, from Targets to results.
- [Simulation](https://boldaxolotl.github.io/booley/docs/flow-sim/): `sim` tests, Test Runs, seeds, verdicts, and `[flows.sim]`.
- [Coverage](https://boldaxolotl.github.io/booley/docs/coverage/): collect, gate, and analyze Verilator coverage.
- [Lint](https://boldaxolotl.github.io/booley/docs/flow-lint/), [ASIC synthesis](https://boldaxolotl.github.io/booley/docs/flow-synth/), [FPGA implementation](https://boldaxolotl.github.io/booley/docs/flow-fpga/): the other three Flows.

**Reference**

- [Config](https://boldaxolotl.github.io/booley/docs/config/): Project-wide `booley.toml` knobs, and where the rest live.
- [Targets and tests](https://boldaxolotl.github.io/booley/docs/targets/): `.core` files, `tests.toml`, and Cocotb Targets.
- [Sandbox](https://boldaxolotl.github.io/booley/docs/sandbox/): container policy, egress, custom images, and commercial EDA tools.
- [Stealth Mode](https://boldaxolotl.github.io/booley/docs/stealth/): keep Booley out of your Git history.
- [CLI reference](https://boldaxolotl.github.io/booley/docs/cli-reference/): every command, Flow, and Specialist.
- [Supported EDA tools](https://boldaxolotl.github.io/booley/docs/supported-eda-tools/): which tools each Flow drives, and where they come from.

**Help**

- [Doctor](https://boldaxolotl.github.io/booley/docs/doctor/): health checks for Booley's build and execution machinery.
- [Troubleshooting](https://boldaxolotl.github.io/booley/docs/troubleshooting/): symptoms, causes, and fixes.
- [Windows first-run problems](https://boldaxolotl.github.io/booley/docs/windows/): Windows-only setup traps.

These pages track the `main` branch, so they can describe changes newer than
your installed release; the [changelog](https://github.com/boldaxolotl/booley/blob/main/CHANGELOG.md)
says what each release contains. The source is [`docs/user/`](https://github.com/boldaxolotl/booley/tree/main/docs/user) on GitHub.
