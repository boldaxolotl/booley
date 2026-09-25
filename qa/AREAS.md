# Capability map

Which mission areas exercise each Booley capability. Use it to spot gaps when
adding to QA; it is a breadth guide, not a pass/fail contract.

| Capability | Mission areas |
|---|---|
| Product artifact installation | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/host-inventory](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/install](missions/uart/MISSION.md) |
| Host bootstrap | [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project initialization | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project scaffolding | [uart/bootstrap-init](missions/uart/MISSION.md) |
| Agent authentication | [uart/auth](missions/uart/MISSION.md) |
| Sandbox lifecycle | [coverage/cov-analyst](missions/coverage/MISSION.md), [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/cleanup](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [uart/cleanup](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md), [uart/runtime-lifecycle](missions/uart/MISSION.md) |
| Project inventory | [picorv32/host-inventory](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| Host EDA provisioning | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| EDA license policy | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| Doctor diagnostics | [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md), [uart/doctor-upgrade](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Product upgrade | [uart/doctor-upgrade](missions/uart/MISSION.md) |
| Host policy | [uart/host-policy](missions/uart/MISSION.md) |
| Local-first operation | [uart/host-policy](missions/uart/MISSION.md), [uart/install](missions/uart/MISSION.md) |
| Project setup | [taxi/setup](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Target catalog | [picorv32/cleanup](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/ticket1](missions/picorv32/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md) |
| Target selection | [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Simulation test protocol | [picorv32/sim-protocol](missions/picorv32/MISSION.md) |
| Cocotb test execution | [taxi/baseline](missions/taxi/MISSION.md) |
| Simulation execution guards | [picorv32/sim-protocol](missions/picorv32/MISSION.md) |
| Project Sandbox Image | [taxi/setup](missions/taxi/MISSION.md) |
| RISC-V toolchain | [picorv32/host-inventory](missions/picorv32/MISSION.md) |
| Vendored core discovery | [taxi/submodules](missions/taxi/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Custom Flow extensions | [uart/interactive-surface](missions/uart/MISSION.md) |
| Interactive Mode | [picorv32/interactive-bwave](missions/picorv32/MISSION.md), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [uart/gui-client](missions/uart/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| MCP job control | [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Mode-specific MCP surface | [uart/interactive-surface](missions/uart/MISSION.md) |
| Job admission control | [taxi/campaign](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Public capability discovery | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/host-inventory](missions/picorv32/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Simulation Flow | [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/sim-protocol](missions/picorv32/MISSION.md), [taxi/baseline](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md) |
| Simulation Campaign | [coverage/cov-retention](missions/coverage/MISSION.md), [picorv32/sim-campaign](missions/picorv32/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md), [uart/sim-campaign](missions/uart/MISSION.md) |
| Lint Flow | [picorv32/interactive-repair](missions/picorv32/MISSION.md), [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Synthesis Flow | [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| FPGA Flow | [picorv32/ticket2](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| Specialist reviews | [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Mutation testing | [uart/reviews-mutation](missions/uart/MISSION.md) |
| Specialist isolation | [uart/reviews-mutation](missions/uart/MISSION.md) |
| Criterion evidence binding | [coverage/cov-retention](missions/coverage/MISSION.md), [coverage/cov-ticket](missions/coverage/MISSION.md), [picorv32/sim-campaign](missions/picorv32/MISSION.md), [picorv32/ticket1](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Criterion acceptance policy | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md) |
| Metric-threshold Criteria | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [picorv32/lint-synth](missions/picorv32/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md) |
| Ticket authoring | [taxi/ticket1-observability](missions/taxi/MISSION.md) |
| Ticket deliverables | [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [picorv32/ticket1](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md), [uart/repair](missions/uart/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Ticket lifecycle | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket triage | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket Scope | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [picorv32/ticket-create](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md) |
| Ticket baseline | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md), [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Blocked Ticket amendment | [picorv32/amendment-apply-resume](missions/picorv32/MISSION.md), [picorv32/amendment-block-preview](missions/picorv32/MISSION.md) |
| Ticket integration | [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket resilience | [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket concurrency | [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket operator interface | [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket execution policy | [taxi/ticket-machinery](missions/taxi/MISSION.md) |
| Ticket notifications | [uart/sandbox-isolation](missions/uart/MISSION.md) |
| Trace Artifact | [picorv32/interactive-bwave](missions/picorv32/MISSION.md), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Trace registry | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform queries | [picorv32/interactive-bwave](missions/picorv32/MISSION.md), [picorv32/interactive-repair](missions/picorv32/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Waveform time model | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform Viewer | [taxi/bwave](missions/taxi/MISSION.md) |
| Stealth Mode | [picorv32/git-stealth-security](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md) |
| Stealth core projection | [picorv32/git-stealth-security](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md) |
| Git safety policy | [picorv32/git-stealth-security](missions/picorv32/MISSION.md) |
| Sandbox isolation | [coverage/cov-analyst](missions/coverage/MISSION.md), [picorv32/git-stealth-security](missions/picorv32/MISSION.md), [picorv32/vivado-fpga](missions/picorv32/MISSION.md), [taxi/isolation](missions/taxi/MISSION.md), [uart/sandbox-isolation](missions/uart/MISSION.md) |
| Findings Log | [uart/interactive-surface](missions/uart/MISSION.md) |
| Feedback reporting | [uart/interactive-surface](missions/uart/MISSION.md) |
| Documentation usability | [picorv32/host-inventory](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md), [picorv32/ticket-create](missions/picorv32/MISSION.md), [picorv32/ticket2](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [taxi/seed-ticket2](missions/taxi/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [taxi/ticket1-observability](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/feature-ticket](missions/uart/MISSION.md), [uart/gui-client](missions/uart/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Verilator simulation integration | [coverage/cov-collect](missions/coverage/MISSION.md), [taxi/baseline](missions/taxi/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md) |
| Verilator lint integration | [picorv32/setup](missions/picorv32/MISSION.md) |
| Icarus simulation integration | [picorv32/setup](missions/picorv32/MISSION.md) |
| Verible lint integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Yosys logical synthesis integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Slang integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Yosys physical synthesis integration | [picorv32/setup](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| sv2v integration | [picorv32/lint-synth](missions/picorv32/MISSION.md), [picorv32/setup](missions/picorv32/MISSION.md) |
| OpenROAD integration | [picorv32/setup](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| Vivado integration | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| B-Wave with Icarus integration | [picorv32/interactive-repair](missions/picorv32/MISSION.md) |
| B-Wave with Verilator integration | [taxi/bwave](missions/taxi/MISSION.md) |
| Simulation EDA tool resolution | [picorv32/setup](missions/picorv32/MISSION.md) |
| Lint EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Synthesis EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| FPGA EDA tool resolution | [picorv32/vivado-fpga](missions/picorv32/MISSION.md) |
| Native simulation coverage | [coverage/cov-collect](missions/coverage/MISSION.md), [coverage/cov-numeric](missions/coverage/MISSION.md) |
| Coverage Criteria policy | [coverage/cov-policy](missions/coverage/MISSION.md), [coverage/cov-ticket](missions/coverage/MISSION.md) |
| Approved coverage waivers | [coverage/cov-waivers](missions/coverage/MISSION.md) |
| Coverage Campaign storage | [coverage/cov-numeric](missions/coverage/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md), [coverage/cov-storage](missions/coverage/MISSION.md) |
| Coverage Campaign retention | [coverage/cov-retention](missions/coverage/MISSION.md) |
| Coverage Analyst | [coverage/cov-analyst](missions/coverage/MISSION.md) |
| Coverage Analyst evidence boundary | [coverage/cov-analyst](missions/coverage/MISSION.md) |
