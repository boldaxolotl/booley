# Changelog

All notable user-visible changes to Booley are recorded here. Release entries
use stable `MAJOR.MINOR.PATCH` headings so Booley can review an exact upgrade
range from the packaged copy of this file.

Packaged release history starts with the first release that replaces the
Unreleased section with a dated stable-version entry. For older changes, see
[GitHub Releases](https://github.com/boldaxolotl/Booley/releases).

## Unreleased

### Bug fixes

- Host bootstrap now requires Git 2.37.2 or newer, avoiding a Git for Windows
  temporary-name exhaustion failure during large line-ending repairs.
- Session Runtime issuance now finds trusted Booley executables installed in
  Python's per-user scripts directory even when that directory is absent from
  `PATH`.
- Skill reconciliation now always deploys packaged skills to `.agents/skills`
  for Codex, while continuing to deploy to a distinct existing `.claude/skills`
  directory.

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
