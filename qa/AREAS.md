# Capability map

Which mission areas exercise each Booley capability. Use it to spot gaps when
adding to QA; it is a breadth guide, not a pass/fail contract.

Areas marked (optional) are follow-up work outside a mission's primary
timebox. A row whose areas are all optional is not covered by a primary run.

| Capability | Mission areas |
|---|---|
| Product artifact installation | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/host-inventory](missions/picorv32/MISSION.md) (optional), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/install](missions/uart/MISSION.md) |
| Host bootstrap | [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project initialization | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project scaffolding | [uart/bootstrap-init](missions/uart/MISSION.md) |
| Agent authentication | [uart/auth](missions/uart/MISSION.md) |
| Sandbox lifecycle | [coverage/cov-analyst](missions/coverage/MISSION.md), [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/cleanup](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [uart/cleanup](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md) (optional), [uart/runtime-lifecycle](missions/uart/MISSION.md) |
| Project inventory | [picorv32/host-inventory](missions/picorv32/MISSION.md) (optional), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| Host EDA provisioning | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| EDA license policy | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| Doctor diagnostics | [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md), [uart/doctor-upgrade](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md) (optional), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Product upgrade | [uart/doctor-upgrade](missions/uart/MISSION.md) |
| Host policy | [uart/host-policy](missions/uart/MISSION.md) (optional) |
| Local-first operation | [uart/host-policy](missions/uart/MISSION.md) (optional), [uart/install](missions/uart/MISSION.md) |
| Project setup | [taxi/setup](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Target catalog | [picorv32/cleanup](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/goal-evidence](missions/picorv32/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md) |
| Target selection | [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Simulation test protocol | [picorv32/sim-protocol](missions/picorv32/MISSION.md) (optional) |
| Cocotb test execution | [taxi/baseline](missions/taxi/MISSION.md) |
| Simulation execution guards | [picorv32/sim-protocol](missions/picorv32/MISSION.md) (optional) |
| Project Sandbox Image | [taxi/setup](missions/taxi/MISSION.md) |
| RISC-V toolchain | [picorv32/host-inventory](missions/picorv32/MISSION.md) (optional) |
| Vendored core discovery | [taxi/submodules](missions/taxi/MISSION.md) (optional), [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Custom Flow extensions | [uart/interactive-surface](missions/uart/MISSION.md) |
| Interactive Mode | [picorv32/interactive-bwave](missions/picorv32/MISSION.md) (optional), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [uart/gui-client](missions/uart/MISSION.md) (optional), [uart/interactive-surface](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| MCP job control | [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Mode-specific MCP surface | [uart/interactive-surface](missions/uart/MISSION.md) |
| Job admission control | [taxi/campaign](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Public capability discovery | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/host-inventory](missions/picorv32/MISSION.md) (optional), [taxi/interactive](missions/taxi/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Simulation Flow | [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/sim-protocol](missions/picorv32/MISSION.md) (optional), [taxi/baseline](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md) |
| Simulation Campaign | [coverage/cov-retention](missions/coverage/MISSION.md), [picorv32/sim-campaign](missions/picorv32/MISSION.md) (optional), [taxi/campaign](missions/taxi/MISSION.md), [uart/sim-campaign](missions/uart/MISSION.md) |
| Lint Flow | [picorv32/interactive-repair](missions/picorv32/MISSION.md), [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/goal-change](missions/picorv32/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Synthesis Flow | [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/goal-change](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| FPGA Flow | [picorv32/goal-change](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| Specialist reviews | [picorv32/goal-change](missions/picorv32/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Mutation testing | [uart/reviews-mutation](missions/uart/MISSION.md) |
| Specialist isolation | [uart/reviews-mutation](missions/uart/MISSION.md) |
| Goal evidence binding and freshness | [picorv32/goal-evidence](missions/picorv32/MISSION.md), [picorv32/sim-campaign](missions/picorv32/MISSION.md) (optional), [taxi/goal-observability](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md), [coverage/cov-goal](missions/coverage/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Mandatory Goal completion | [picorv32/goal-finish](missions/picorv32/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md), [coverage/cov-goal](missions/coverage/MISSION.md) |
| Metric-threshold Goals | [picorv32/goal-change](missions/picorv32/MISSION.md), [picorv32/goal-evidence](missions/picorv32/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [coverage/cov-policy](missions/coverage/MISSION.md) |
| Goal entry and refusals | [picorv32/goal-enter](missions/picorv32/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md) |
| Goalsets and ad-hoc Goals | [picorv32/goal-enter](missions/picorv32/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md) |
| Protected Inputs and discarded evidence | [picorv32/goal-evidence](missions/picorv32/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md) |
| Goal Change Proposals and approval | [picorv32/goal-change](missions/picorv32/MISSION.md), [coverage/cov-goal](missions/coverage/MISSION.md) |
| Silence leaves proposals pending | [picorv32/goal-change](missions/picorv32/MISSION.md) |
| Finish and Review Package | [picorv32/goal-finish](missions/picorv32/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md) |
| Base-relative Goal evidence | [picorv32/goal-change](missions/picorv32/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md) |
| Explicit abandon | [picorv32/goal-resume-after-crash](missions/picorv32/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md) |
| Operator integration (paired when printed) | [picorv32/goal-finish](missions/picorv32/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Crash resume and idempotence | [picorv32/goal-resume-after-crash](missions/picorv32/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md) |
| Concurrent Goal children and Dashboard | [taxi/goal-operations](missions/taxi/MISSION.md) |
| Doctor Goal warnings | [taxi/goal-operations](missions/taxi/MISSION.md) |
| Trace Artifact | [picorv32/interactive-bwave](missions/picorv32/MISSION.md) (optional), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Trace registry | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform queries | [picorv32/interactive-bwave](missions/picorv32/MISSION.md) (optional), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Waveform time model | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform Viewer | [taxi/bwave](missions/taxi/MISSION.md) |
| Stealth Mode | [picorv32/git-stealth-security](missions/picorv32/MISSION.md) (optional), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md) |
| Stealth core projection | [picorv32/git-stealth-security](missions/picorv32/MISSION.md) (optional), [picorv32/setup](missions/picorv32/MISSION.md) |
| Git safety policy | [picorv32/git-stealth-security](missions/picorv32/MISSION.md) (optional) |
| Sandbox isolation | [coverage/cov-analyst](missions/coverage/MISSION.md), [picorv32/git-stealth-security](missions/picorv32/MISSION.md) (optional), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional), [taxi/isolation](missions/taxi/MISSION.md), [uart/sandbox-isolation](missions/uart/MISSION.md) |
| Findings Log | [uart/interactive-surface](missions/uart/MISSION.md) |
| Feedback reporting | [uart/interactive-surface](missions/uart/MISSION.md) |
| Documentation usability | [picorv32/host-inventory](missions/picorv32/MISSION.md) (optional), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/goal-enter](missions/picorv32/MISSION.md), [picorv32/goal-change](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/gui-client](missions/uart/MISSION.md) (optional), [uart/interactive-surface](missions/uart/MISSION.md) |
| Verilator simulation integration | [coverage/cov-collect](missions/coverage/MISSION.md), [taxi/baseline](missions/taxi/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md) |
| Verilator lint integration | [picorv32/setup](missions/picorv32/MISSION.md) |
| Icarus simulation integration | [picorv32/setup](missions/picorv32/MISSION.md) |
| Verible lint integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Yosys logical synthesis integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Slang integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Yosys physical synthesis integration | [picorv32/setup](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| sv2v integration | [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md) |
| OpenROAD integration | [picorv32/setup](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| Vivado integration | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| B-Wave with Icarus integration | [picorv32/interactive-repair](missions/picorv32/MISSION.md) |
| B-Wave with Verilator integration | [taxi/bwave](missions/taxi/MISSION.md) |
| Simulation EDA tool resolution | [picorv32/setup](missions/picorv32/MISSION.md) |
| Lint EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Synthesis EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| FPGA EDA tool resolution | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) (optional) |
| Native simulation coverage | [coverage/cov-collect](missions/coverage/MISSION.md), [coverage/cov-numeric](missions/coverage/MISSION.md) |
| Coverage Goals and policy | [coverage/cov-policy](missions/coverage/MISSION.md), [coverage/cov-goal](missions/coverage/MISSION.md) |
| Approved coverage waivers | [coverage/cov-waivers](missions/coverage/MISSION.md) |
| Coverage Campaign storage | [coverage/cov-numeric](missions/coverage/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md), [coverage/cov-storage](missions/coverage/MISSION.md) |
| Coverage Campaign retention | [coverage/cov-retention](missions/coverage/MISSION.md) |
| Coverage Analyst | [coverage/cov-analyst](missions/coverage/MISSION.md) |
| Coverage Analyst evidence boundary | [coverage/cov-analyst](missions/coverage/MISSION.md) |
