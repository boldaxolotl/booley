# Keep Project Root as the default Simulation working directory

Status: accepted

## Context

FuseSoC normally runs a simulator in its build `work_root`, where fileset
`copyto:` inputs are available for relative-path reads. Booley defaults to the
Project Root. [Issue #1536](https://github.com/boldaxolotl/booley/issues/1536)
asked whether this difference required a new default or per-Target inference.
Its premise that `copyto:` inputs were unavailable was outdated: Booley already
exposes declared runtime inputs in the resolved run directory.

## Decision

Keep Project Root as the default, as chosen in the
[#1536 decision](https://github.com/boldaxolotl/booley/issues/1536#issuecomment-6191855321).
Projects can select a literal directory with `[flows.sim].run_cwd`. Durable
ordinary-HDL Simulation Campaigns also support `{campaign}`, `{target}`, `{test}`,
and `{attempt}` placeholders for Booley-owned attempt directories. The
[configuration reference](../user/FLOW-SIM.md#sim-working-directory-flowssimrun_cwd)
describes their lifecycle and validation.

Expose fileset `copyto:` runtime inputs in that resolved directory so
FuseSoC-native relative reads work without changing the default. Non-campaign
runs use temporary symlink overlays; Simulation Campaign runs use per-attempt,
SHA-256-authenticated copies and expose them in the run directory.

## Rejected alternatives

**B: Default to the build `work_root`.** Simulation Campaigns execute from
per-attempt executable snapshots containing authenticated copies of the
Simulator Bundle's declared executable artifacts. Those artifact files are
read-only. Using the snapshot as cwd would mix simulator outputs into this
authenticated execution surface; using the shared `work_root` would reintroduce
mutable-bundle execution. Changing the default would also disrupt Projects
whose testbenches read Project-relative paths or whose Pre-Sim Commands stage
files into `BOOLEY_RUN_CWD` under the existing directory assumptions.

**C: Infer cwd per Target from fileset `copyto:` declarations.** Input exposure
already supports those declarations. Inference would make cwd policy depend on
fileset contents, adding hidden behavior without solving a remaining input
availability problem.

## Consequences

- Project-relative reads keep their existing default. Projects that need a
  different base for relative paths select it explicitly through `run_cwd`.
- Pre-Sim Commands keep Project Root as their cwd; `BOOLEY_RUN_CWD` identifies
  the resolved simulator run directory for hooks that prepare runtime files.
- An identical existing runtime-input file is retained. A differing file at
  the destination causes a conflict at launch instead of being overwritten.
- A Doctor warning for conflicting `copyto:` destinations is tracked separately
  in [#1592](https://github.com/boldaxolotl/booley/issues/1592).
