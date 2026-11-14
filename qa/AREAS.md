# Capability map

Which mission areas exercise each Booley capability (the Capability Map in
`GLOSSARY.md`). Read it when adding to QA, as the `booley-add-to-qa` skill
does, to spot gaps and place new work. It is a breadth guide, not a pass/fail
contract or execution evidence; each mission says how its areas run.

Each link names `mission/area` and points at that mission's file. An area is
marked (optional) exactly when its mission heading calls it an optional
follow-up, outside the mission's primary timebox, so a row whose areas are all
optional is not covered by a primary run.
`test_capability_map_links_resolve_to_mission_areas` in
`tests/qa/test_missions.py` checks both rules.

| Capability | Mission areas |
|---|---|
| Product artifact installation | [coverage/cov-collect](missions/coverage/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/install](missions/uart/MISSION.md) |
| Host bootstrap | [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project initialization | [coverage/cov-collect](missions/coverage/MISSION.md), [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md) |
| Project scaffolding | [uart/bootstrap-init](missions/uart/MISSION.md) |
| Agent authentication | [uart/auth](missions/uart/MISSION.md) |
| Sandbox lifecycle | [coverage/cov-analyst](missions/coverage/MISSION.md), [coverage/cov-collect](missions/coverage/MISSION.md), [gui/stop-resume](missions/gui/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [uart/cleanup](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md) (optional), [uart/runtime-lifecycle](missions/uart/MISSION.md) |
| Project inventory | [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional), [uart/project-inventory](missions/uart/MISSION.md) (optional) |
| Host EDA provisioning | [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional) |
| EDA license policy | [scheduler/sched-seats](missions/scheduler/MISSION.md), [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional) |
| Doctor diagnostics | [gui/setup](missions/gui/MISSION.md), [picorv32/baseline](missions/picorv32/MISSION.md), [scheduler/sched-cutover](missions/scheduler/MISSION.md), [scheduler/sched-seats](missions/scheduler/MISSION.md), [scheduler/setup](missions/scheduler/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md), [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional), [uart/doctor-upgrade](missions/uart/MISSION.md), [uart/external-image](missions/uart/MISSION.md) (optional), [uart/final-regression](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Product upgrade | [uart/doctor-upgrade](missions/uart/MISSION.md) |
| Host policy | [uart/host-policy](missions/uart/MISSION.md) (optional) |
| Local-first operation | [uart/host-policy](missions/uart/MISSION.md) (optional), [uart/install](missions/uart/MISSION.md) |
| Project setup | [taxi/setup](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Target catalog | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Target selection | [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Simulation test protocol | [uart/sim-protocol](missions/uart/MISSION.md) (optional) |
| Cocotb test execution | [taxi/baseline](missions/taxi/MISSION.md) |
| Simulation execution guards | [uart/sim-protocol](missions/uart/MISSION.md) (optional) |
| Project Sandbox Image | [taxi/setup](missions/taxi/MISSION.md) |
| RISC-V toolchain | [picorv32/baseline](missions/picorv32/MISSION.md) |
| Vendored core discovery | [taxi/submodules](missions/taxi/MISSION.md) (optional), [taxi/targets-doctor](missions/taxi/MISSION.md) |
| Custom Flow extensions | [scheduler/setup](missions/scheduler/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Interactive Mode | [gui/dashboard-live](missions/gui/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| MCP job control | [taxi/bwave](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Mode-specific MCP surface | [uart/interactive-surface](missions/uart/MISSION.md) |
| Job admission control | [scheduler/sched-admission](missions/scheduler/MISSION.md), [scheduler/sched-fullchip](missions/scheduler/MISSION.md) (optional), [scheduler/sched-jump](missions/scheduler/MISSION.md), [scheduler/sched-records](missions/scheduler/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md) |
| Job resource history | [scheduler/sched-history](missions/scheduler/MISSION.md) |
| Memory reservation shrinking | [scheduler/sched-shrink](missions/scheduler/MISSION.md) |
| Out-of-memory requeue | [scheduler/sched-oom](missions/scheduler/MISSION.md) |
| Seat Caps | [scheduler/sched-seats](missions/scheduler/MISSION.md) |
| Build thread counts | [scheduler/sched-threads](missions/scheduler/MISSION.md) |
| Retired Job Slots keys | [scheduler/sched-cutover](missions/scheduler/MISSION.md) |
| Public capability discovery | [coverage/cov-collect](missions/coverage/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Simulation Flow | [gui/setup](missions/gui/MISSION.md), [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/baseline](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/sim-protocol](missions/uart/MISSION.md) (optional) |
| Simulation Campaign | [coverage/cov-retention](missions/coverage/MISSION.md), [scheduler/sched-records](missions/scheduler/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md), [uart/sim-campaign](missions/uart/MISSION.md) |
| Lint Flow | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Synthesis Flow | [picorv32/baseline](missions/picorv32/MISSION.md), [scheduler/sched-shrink](missions/scheduler/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [uart/final-regression](missions/uart/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| FPGA Flow | [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional) |
| Specialist reviews | [taxi/goal-repair](missions/taxi/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Mutation testing | [mutation/mut-determinism](missions/mutation/MISSION.md), [mutation/mut-failures](missions/mutation/MISSION.md), [mutation/mut-removed](missions/mutation/MISSION.md), [mutation/mut-scope](missions/mutation/MISSION.md), [mutation/mut-scoring](missions/mutation/MISSION.md), [mutation/mut-selection](missions/mutation/MISSION.md), [mutation/mut-sites](missions/mutation/MISSION.md), [mutation/mut-timeouts](missions/mutation/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [uart/reviews-mutation](missions/uart/MISSION.md) |
| Specialist isolation | [uart/reviews-mutation](missions/uart/MISSION.md) |
| Goal evidence binding and freshness | [coverage/cov-goal](missions/coverage/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Mandatory Goal completion | [coverage/cov-goal](missions/coverage/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md) |
| Metric-threshold Goals | [coverage/cov-policy](missions/coverage/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md) |
| Goal entry and refusals | [taxi/goal-operations](missions/taxi/MISSION.md) |
| Goalsets and ad-hoc Goals | [taxi/goal-observability](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Protected Inputs and discarded evidence | [taxi/goal-operations](missions/taxi/MISSION.md), [uart/goal-repair](missions/uart/MISSION.md) |
| Goal Change Proposals and approval | [coverage/cov-goal](missions/coverage/MISSION.md), [uart/goal-resume](missions/uart/MISSION.md) (optional) |
| Silence leaves proposals pending | [uart/goal-resume](missions/uart/MISSION.md) (optional) |
| Finish and Review Package | [gui/goal-finish](missions/gui/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Base-relative Goal evidence | [taxi/goal-repair](missions/taxi/MISSION.md) |
| Explicit abandon | [taxi/goal-operations](missions/taxi/MISSION.md), [uart/goal-repair](missions/uart/MISSION.md), [uart/goal-resume](missions/uart/MISSION.md) (optional) |
| Operator integration (paired when printed) | [taxi/goal-observability](missions/taxi/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md) |
| Crash resume and idempotence | [coverage/cov-retention](missions/coverage/MISSION.md), [gui/stop-resume](missions/gui/MISSION.md), [uart/goal-resume](missions/uart/MISSION.md) (optional) |
| Concurrent Goal children and Dashboard | [gui/dashboard-live](missions/gui/MISSION.md), [taxi/goal-operations](missions/taxi/MISSION.md) |
| Doctor Goal warnings | [taxi/goal-operations](missions/taxi/MISSION.md) |
| Trace Artifact | [taxi/bwave](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Trace registry | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform queries | [gui/bwave-gui](missions/gui/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Waveform time model | [taxi/bwave](missions/taxi/MISSION.md) |
| Waveform Viewer | [gui/bwave-gui](missions/gui/MISSION.md), [taxi/bwave](missions/taxi/MISSION.md) |
| Codespaces onboarding | [gui/docs-site](missions/gui/MISSION.md), [gui/setup](missions/gui/MISSION.md) |
| Dashboard | [gui/dashboard-default](missions/gui/MISSION.md), [gui/dashboard-live](missions/gui/MISSION.md), [gui/matrix](missions/gui/MISSION.md), [gui/stop-resume](missions/gui/MISSION.md) |
| Terminal output rendering | [gui/matrix](missions/gui/MISSION.md), [gui/terminal-output](missions/gui/MISSION.md) |
| Live Preview reports | [gui/live-preview](missions/gui/MISSION.md) |
| Stealth Mode | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/bootstrap-init](missions/uart/MISSION.md), [uart/final-regression](missions/uart/MISSION.md) |
| Stealth core projection | [picorv32/baseline](missions/picorv32/MISSION.md) |
| Git safety policy | [picorv32/baseline](missions/picorv32/MISSION.md) |
| Sandbox isolation | [coverage/cov-analyst](missions/coverage/MISSION.md), [taxi/isolation](missions/taxi/MISSION.md), [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional), [uart/sandbox-isolation](missions/uart/MISSION.md) |
| Findings Log | [uart/interactive-surface](missions/uart/MISSION.md) |
| Feedback reporting | [uart/interactive-surface](missions/uart/MISSION.md) |
| Documentation usability | [gui/docs-site](missions/gui/MISSION.md), [gui/setup](missions/gui/MISSION.md), [picorv32/baseline](missions/picorv32/MISSION.md), [picorv32/hunt](missions/picorv32/MISSION.md), [taxi/cleanup](missions/taxi/MISSION.md), [taxi/final-regression](missions/taxi/MISSION.md), [taxi/goal-observability](missions/taxi/MISSION.md), [taxi/goal-repair](missions/taxi/MISSION.md), [taxi/interactive](missions/taxi/MISSION.md), [taxi/setup](missions/taxi/MISSION.md), [uart/evaluate](missions/uart/MISSION.md), [uart/goal-feature](missions/uart/MISSION.md), [uart/interactive-surface](missions/uart/MISSION.md) |
| Verilator simulation integration | [coverage/cov-collect](missions/coverage/MISSION.md), [taxi/baseline](missions/taxi/MISSION.md), [taxi/campaign](missions/taxi/MISSION.md) |
| Verilator lint integration | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Icarus simulation integration | [uart/sim-campaign](missions/uart/MISSION.md) |
| Verible lint integration | [taxi/lint-synth](missions/taxi/MISSION.md), [uart/setup-interactive](missions/uart/MISSION.md) |
| Yosys logical synthesis integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Slang integration | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Yosys physical synthesis integration | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| sv2v integration | [picorv32/baseline](missions/picorv32/MISSION.md) |
| OpenROAD integration | [picorv32/baseline](missions/picorv32/MISSION.md), [taxi/lint-synth](missions/taxi/MISSION.md) |
| Vivado integration | [scheduler/sched-seats](missions/scheduler/MISSION.md), [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional) |
| B-Wave with Verilator integration | [taxi/bwave](missions/taxi/MISSION.md) |
| Simulation EDA tool resolution | [picorv32/baseline](missions/picorv32/MISSION.md) |
| Lint EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| Synthesis EDA tool resolution | [taxi/lint-synth](missions/taxi/MISSION.md) |
| FPGA EDA tool resolution | [taxi/vivado-fpga](missions/taxi/MISSION.md) (optional) |
| Native simulation coverage | [coverage/cov-collect](missions/coverage/MISSION.md), [coverage/cov-numeric](missions/coverage/MISSION.md) |
| Coverage Goals and policy | [coverage/cov-goal](missions/coverage/MISSION.md), [coverage/cov-policy](missions/coverage/MISSION.md) |
| Approved coverage waivers | [coverage/cov-waivers](missions/coverage/MISSION.md) |
| Coverage Campaign storage | [coverage/cov-numeric](missions/coverage/MISSION.md), [coverage/cov-retention](missions/coverage/MISSION.md), [coverage/cov-storage](missions/coverage/MISSION.md) |
| Coverage Campaign retention | [coverage/cov-retention](missions/coverage/MISSION.md) |
| Coverage Analyst | [coverage/cov-analyst](missions/coverage/MISSION.md) |
| Coverage Analyst evidence boundary | [coverage/cov-analyst](missions/coverage/MISSION.md) |
