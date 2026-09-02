# Changelog

All notable user-visible changes to Booley are recorded here. Release entries
use stable `MAJOR.MINOR.PATCH` headings so Booley can review an exact upgrade
range from the packaged copy of this file.

Packaged release history starts with the first release that replaces the
Unreleased section with a dated stable-version entry. For older changes, see
[GitHub Releases](https://github.com/boldaxolotl/Booley/releases).

## 0.2.11 - 02 SEP 2026

### Quality of life

- Added the host-owned Project Inventory and `booley projects` command, with
  shared discovery, status, access-grant, and JSON views. Help text now marks
  commands by host or Session Runtime.
- FPGA setup now exposes target-aware Doctor probes and dry-run checks across
  the CLI and MCP surfaces, including clearer Vivado and board-target guidance.
- Updated the Session Runtime to Verible v0.0-4163-g6cce8f19, Claude Code
  2.1.258, and Codex CLI 0.152.1, and refreshed the immutable Python and Docker
  CLI bases used by sidecars.
- Version-change warnings are more prominent during startup, and the demo
  guidance now links directly to feedback.

### Bug fixes

- Host bootstrap now requires Git 2.37.2 or newer, avoiding a Git for Windows
  temporary-name exhaustion failure during large line-ending repairs.
- Session Runtime issuance now finds trusted Booley executables installed in
  Python's per-user scripts directory even when that directory is absent from
  `PATH`.
- Skill reconciliation now always deploys packaged skills to `.agents/skills`
  for Codex, while continuing to deploy to a distinct existing `.claude/skills`
  directory.
- `booley session refresh` now parks the target Session Runtime, reconciles its
  dependencies, and restores it in order instead of refreshing around a live
  or partially stopped session.
- Source checkouts no longer acquire Project or Stealth policy accidentally;
  repository classification, hook installation, and managed-state placement
  now preserve the source-checkout boundary.
- Session Image provenance is now scoped to the image that actually runs the
  Project, avoiding stale rebuild prompts from unrelated images.
- Stealth projects now keep core projections and FPGA target metadata within
  their protected project state.

### Upgrade notes

- Percentage-based acceptance-criterion values must include an explicit `%`
  suffix, for example `cycle_count_reduce_at_least: 5%`.
- Custom MCP clients must negotiate protocol version `2026-07-28`. Booley's
  built-in Claude Code and Codex configurations are updated automatically.
- Host bootstrap now requires Git 2.37.2 or newer. The complete demo stack
  requires at least 21 GB of free storage.

## 0.2.10 - 01 SEP 2026

### New features

- Bare `booley` now opens the Project's configured Claude Code or Codex chat;
  `booley chat` is the explicit equivalent and `booley --help` remains the
  command reference.
- Added Project-independent `booley bootstrap` for host prerequisites, skills,
  the shared PDK cache, the base Session Image, and global proxy and reaper
  services. `booley init` performs the same reconciliation when needed.
- Added durable, version-aware upgrade review state with scriptable status and
  compare-and-swap acknowledgment. Doctor and Session Runtime startup identify
  pending or stale reviews; `/booley-heal` reviews the exact packaged changelog
  range and acknowledges it only after verification.

### Quality of life

- Updated the Session Runtime to Node.js 24.20.0 and Claude Code 2.1.252.
- Updated the egress proxy, FlexNet relay, and reaper sidecars to Python 3.14.7,
  with tests for CONNECT streaming, relay forwarding, owned-container cleanup,
  unavailable-daemon handling, and container hardening.
- Replaced the unavailable historical OpenROAD package with the official 26Q3
  OCI channel at an immutable digest. Logical and physical synthesis matched
  the 0.2.9 area, utilization, cell count, and netlist results.
- The RISC-V image now pins Spike to a validated upstream snapshot and runs its
  upstream test suite during the build. The PicoRV32 release smoke now tests
  the project's public `main` commit rather than a private CI-only branch.
- Documentation now distinguishes simulation, lint, synthesis, and FPGA
  implementation as separate Booley Flows. It also describes the current stock
  VS Code interface and agent-written development process, and expands the
  Ticket Mode and CI roadmap.

### Bug fixes

- Claude Code and Codex now install their required Linux/x64 native artifacts
  explicitly so optional-package failures cannot leave unusable launchers.
- Updated to cocotb 2.1.0 and its Icarus GPI contract while retaining cocotb
  1.x and 2.0 compatibility. Production-image Icarus and Verilator cocotb flows
  now run in CI.
- Confidential-content pre-push checks now validate destination refs and scan
  changed tree entries. They still cover newly exposed history, merges,
  renames, deletions, and malformed input.
- Runtime-image builds now use bounded pip download timeouts and retries.

### Upgrade notes

- Run `booley bootstrap` once after upgrading. Existing Project
  `booley.toml [interactive]` host-policy fields are retired; move them to the
  host policy file named by `booley init` or `booley doctor`.
- When Booley reports a version change, invoke `/booley-heal` to review these
  notes, repair drift, and acknowledge the upgrade.
