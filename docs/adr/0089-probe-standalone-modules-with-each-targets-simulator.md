# Probe standalone modules with each Target's simulator

Status: accepted (2026-11-10 plan of
[#2710](https://github.com/boldaxolotl/booley/issues/2710))

Supersedes the fixed-frontend choice of ADR 0042 (the standalone-elaboration
check probes with Icarus) and the `[flows.sim].standalone_frontend` workaround
added after the fpu port (F-25). ADR 0042 is no longer in this directory.

## Context

The `elaborate_standalone` sweep compiles each reusable RTL module on its own.
ADR 0042 fixed one cheap compiler, Icarus. The fpu port showed that
`iverilog -g2012` rejects SystemVerilog that Verilator, the compiler the
Targets build with, accepts. `standalone_frontend` (`auto|iverilog|verilator`)
then chose one compiler for the whole sweep, with `auto` preferring Verilator
when on PATH. One compiler for a whole project is wrong whenever Targets
simulate with different tools: an Icarus-only project was probed with Verilator
and a Verilator-only project with Icarus whenever Verilator was missing, and a
parse error from the other compiler was graded "ungraded" (exit 2), hiding real
findings.

## Decision

- **The probe follows each Target's simulator.** A module in an Icarus Target's
  fileset is probed with Icarus, in a Verilator Target's with Verilator. A
  module shared by both is probed by both; a failure under either fails the
  sweep and names the frontend.
- **Same compiler means a finding.** A same-compiler parse error is a design
  finding (exit 1), never "ungraded".
- **Fallback for other simulators.** A module used only by a Target whose
  simulator the probe cannot drive is probed with Verilator, or Icarus if
  Verilator is absent. Only there, and only when the per-Target builds passed,
  a parse error is graded "ungraded". The sim Flow can select only Icarus and
  Verilator Targets today, so this rule is kept for future commercial
  simulator Targets and is exercised by stubbed tests only.
- **Per-probe prerequisites.** A probe compiles only the shared package and
  interface files of the Targets that contribute its frontend.
- **Retired key.** `[flows.<flow>].standalone_frontend` is a hard migration
  error for every Flow, like `[flows.elab]`; the message says to delete it.
- **Reporting.** The criterion key, verdict and exit codes are unchanged.
  `detail.standalone.frontend` stays a string (the `+`-joined frontends) and a
  `probes` grouping is added; no report schema bump.

## Consequences

- A project mixing Icarus and Verilator Targets compiles shared modules twice,
  so the sweep costs up to 2x. The sweep keeps one shared timeout; a sweep that
  fit before may now time out (exit 2).
- Single-simulator projects keep the verdict, exit codes, and cost. An
  Icarus-only project that used to be probed with Verilator is now probed with
  Icarus, so a parse error is a finding and any other compiler-specific
  result follows Icarus.
- Users with `standalone_frontend` set must delete it before any Flow runs.
