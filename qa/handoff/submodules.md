# Taxi's disposable submodule companion Project

The maintainer approved this companion exercise in the handoff discussion on
2026-09-08. It is a named phase of the Taxi scenario, with 40 minutes of work and
its resources included in Taxi cleanup. It leaves the pinned direct Taxi clone,
its symlink, Setup, hardware workloads and accepted verification assets unchanged.
It is not a fourth IP journey or a consumer wrapper around Taxi.

Public authority: [Submodules](../../docs/user/CONFIG.md#submodules), reconciled
against the exact published Booley release documentation at execution. The phase
closes the submodule part of inventory P-09; normal flat/vendored source handling
continues to belong to the real IP journeys.

## Fixture and pre-run authority

Create only beneath the run-owned companion root and its operator-owned fixture
source directory. Register all Projects, local repositories, branches, worktrees
and runtimes in the ownership ledger. A separate paired `.booley_project` Git
repository is intentional and independently owned. Use the same exact published
Booley release and image identity as the Taxi run. No package installation,
network Git fetch, external push or source edit in the real Taxi Project is allowed.

The suite supplies a deterministic construction recipe, not a dependency on a
new public repository. Use fixed author/committer name `Booley QA`, email
`qa@example.invalid`, timestamp `2000-01-01T00:00:00Z`, UTF-8/LF files and fixed
commit messages for fixture commits. Freeze the actual object IDs, Git object
format, file hashes and repository layout as producing-step evidence. Exact
SHA values may differ by object format; every later assertion uses the recorded
objects and independently verifies their committed contents.

```text
companion/                         # initialized outer Project repository
  fixture.core                    # two HDL simulation Targets plus root-only synthesis
  rtl/transport.sv                # root-only synthesizable transport fixture
  tb/fixture_tb.sv                 # expects the selected historical constants
  deps/data/                      # top-level gitlink
    rtl/data.sv                   # constant DATA changes from 16'h1357 to 16'h2468
    nested/leaf/                  # recursive gitlink
      rtl/leaf.sv                 # constant LEAF changes from 8'h12 to 8'h34
  deps/unselected/                # second top-level gitlink, harmless text only
  .booley_project/                # separate paired Git repository
    deps/control/                 # independent top-level gitlink
      fixture-control.txt         # constant control-v1/control-v2
```

Each dependency has two fully local committed revisions A and B. Both versions
remain in its complete non-shallow local object database. Outer baseline A pins
`deps/data` A (which pins leaf A); outer candidate B pins data B/leaf B.
The corresponding paired Project commits pin control A/B. The initialized main
Project starts clean at candidate B. Baseline-relative work must reconstruct A
from local objects even though the initialized source checkouts are at B.
The testbench instantiates the RTL module in `deps/data/rtl/data.sv`; that module
instantiates the RTL module in `deps/data/nested/leaf/rtl/leaf.sv`. The leaf's output
reaches a top-level signal that the testbench checks together with DATA. These
are required compiled sources and live simulation dependencies, not text files
that are only inspected by the coordinator. There are no duplicate module
definitions, fallback constants, precompiled dependency libraries or optional
filesets that could let a missing dependency pass.

The simulation consumes DATA and LEAF through this instantiated hierarchy;
`sim_submodule_fixture_a` and `sim_submodule_fixture_b` have immutable expected
constants for their respective revisions. Both versions are valid passing designs.
`synth_submodule_transport` uses logical synthesis of the root-only transport
module, independent of selected dependency source paths. Baseline-relative
synthesis through this Target triggers public workspace reconstruction even in
selection probes that intentionally omit the data submodule; those probes do
not falsely require a simulation whose inputs they excluded.

The maintainer specifically requires proof that a missing required submodule
breaks simulation. Therefore the root-only synthesis checks establish selection
and reconstruction behavior only. They cannot satisfy the functional dependency
checks below. The required chain is passing simulation, missing dependency,
failed Simulation Flow, exact restoration, and a fresh passing simulation.

Initialize all sources locally before recording the precondition. Then set each
committed `.gitmodules` URL to a deliberately unreachable fixture-only SSH
locator under `example.invalid`; no credentials are supplied. Normal runtime
default-deny egress remains active. Capture fixture Git subprocess diagnostics
without secret-bearing environment values. Success must not depend on URLs,
remote configuration, a shared object store or a working-tree copy.

## Ordered checks

All IDs below belong to scenario `taxi-10g-mac-port-evolution` and use the suffixes
shown. Evidence is captured immediately after each stimulus and before restoration.
Keep the final production profile list explicit. Each negative variant starts
from a fresh known-good disposable fixture state, varies one precondition, and
is restored before the next check. Fixture creation itself is not product proof.

| Check suffix | Product stimulus | Expected outcome and evidence |
| --- | --- | --- |
| `submodules.fixture` | Initialize the companion Project and complete local A/B hierarchy; inspect source Git status/history and current gitlinks. | Clean, present, non-shallow repositories; object closure and actual source/baseline identities retained. |
| `submodules.ticket-current` | Create and execute a tiny Verification Ticket at candidate B through the ordinary Ticket Create and `booley run` path; its simulation Criterion consumes DATA/LEAF B. | Ticket Workspace contains exact destination gitlinks B; passing simulation and immutable Acceptance Basis/report identify B and the paired repository. |
| `submodules.simulation-dependency-baseline` | Run `sim_submodule_fixture_b` through the public Simulation Flow with the complete dependency hierarchy, from a fresh build. | Fresh passing result and self-checking output prove DATA=16'h2468 and LEAF=8'h34. Resolved files, compiler inputs and elaborated hierarchy identify both required submodule RTL files; retain the testbench assertion and trace or simulator output proving their outputs were consumed. |
| `submodules.simulation-missing-top` | In the same disposable fixture after that pass, move only `deps/data` out of all Project/build search paths; leave its gitlink, Target, testbench, expected values and other inputs unchanged. Invoke the same Simulation Flow again, including with the preceding successful build cache present. | The new invocation fails because required submodule/source content is absent. A configuration/materialization/compile rejection before simulator launch is valid failure evidence; a timeout, unrelated error, skipped test or reused passing artifact is not. Capture the missing path, fresh invocation identity, nonzero result and diagnostic chain. No fresh passing simulation or satisfied Simulation Criterion may be produced. |
| `submodules.simulation-restore-top` | Restore the exact recorded `deps/data` hierarchy locally, with no fetch or stub substitution, and rerun the same Target. | Original dependency commits/file hashes restored and a fresh passing self-checking simulation proves both constants again; retain both the prior failure and this recovery. |
| `submodules.simulation-missing-nested` | From the restored passing fixture, move only `deps/data/nested/leaf` out of all source/search paths while retaining the parent module and all acceptance inputs; invoke the same Simulation Flow. | New invocation fails specifically on the required recursive dependency, including with a prior successful build available. Preserve the diagnostic, source manifest and absence of any fresh passing result/criterion credit. Parent-submodule presence alone cannot satisfy this check. |
| `submodules.simulation-restore-nested` | Restore the exact leaf commit and rerun the unchanged Target. | Fresh pass proves the nested module's output is consumed again; source identity and original negative result retained. |
| `submodules.simulation-missing-cold` | Repeat the required-top-submodule absence in a separate cold disposable fixture with no prior compiled products, using the same Target/testbench contract. | Same dependency-specific nonzero Simulation Flow outcome. This distinguishes actual missing-source detection from any warm-cache side effect. Restore the fixture before its normal recovery/cleanup checks. |
| `submodules.baseline-historical` | Run baseline-relative logical synthesis on `synth_submodule_transport` comparing baseline A with candidate B. | Baseline workspace contains A/leaf A/control A while initialized sources remain B; valid results and artifact contents identify each directed side, never the main Project's newer checkout. |
| `submodules.standalone` | Inspect the repositories materialized by those product operations before their controlled cleanup. | Every selected outer, nested and paired submodule is detached at its destination commit, has its own `.git` directory and local object closure, no remotes, no `.git` pointer/alternates to source storage. Retain Git identity/config and filesystem evidence. |
| `submodules.offline` | Perform the same reconstruction with unreachable committed URLs and default-deny egress. | Reconstruction and Flow/Ticket succeed from local objects; retain URL identity, egress configuration, available Git invocation diagnostics and destination contents. Failure to access an SSH URL is not the expected success path. |
| `submodules.default-all` | Omit `[submodules].paths` and materialize a destination with both top-level gitlinks. | Both outer gitlinks and recursively selected children appear at exact pins; record tree inventory. |
| `submodules.select-one` | Set `paths = ["deps/data"]` for an independently authored generation and materialize. | Only data and its nested leaf materialize in the outer workspace; unselected directory remains an empty gitlink directory. |
| `submodules.select-none` | Set an explicit empty list and materialize. | No outer top-level submodule is materialized; no simulation requiring excluded data is claimed to pass. |
| `submodules.simulation-excluded-dependency` | In that explicitly empty-selection fixture, invoke the simulation that still requires data/leaf inside the materialized workspace. | Simulation fails on the missing required source; successful selection/reconstruction of an intentionally empty set must not masquerade as successful hardware simulation. Retain the unchanged source references, empty dependency paths, fresh nonzero outcome and no satisfied Simulation Criterion. |
| `submodules.selection-intersection` | Include an allowed path not present as a gitlink in that destination revision. | Selection intersects that revision's gitlinks; no invented repository or remote lookup. |
| `submodules.paired-always` | With outer `paths = []`, materialize a destination whose paired Project repository pins control. | Paired repository's control submodule still materializes recursively at its own destination pin. |
| `submodules.missing` | Remove one initialized source submodule only in a disposable variant, then trigger workspace materialization. | Hard failure identifies missing source submodule and initialization remedy; no fabricated empty success. |
| `submodules.dirty` | Change a tracked source file without committing in a disposable variant, then materialize. | Hard failure identifies dirty source; source bytes are retained as fixture evidence and not silently reset by Booley. |
| `submodules.shallow` | Supply a demonstrably shallow local source variant and materialize. | Hard failure identifies shallow repository and complete-history remedy. |
| `submodules.incomplete-objects` | In a disposable copy only, remove a known required reachable historical object while preserving the current worktree, then materialize the historical destination. | Hard failure identifies incomplete local objects; no fallback fetch or newer-checkout substitution. Capture exact missing object identity and object-check result. |
| `submodules.rollback` | Cause a later nested materialization to fail after a prior repository was created; place a uniquely hashed pre-existing sentinel in an unrelated destination beforehand. | Attempt-created repositories are rolled back; pre-existing sentinel/content remain byte-identical. Failure and before/after path inventories retained. |
| `submodules.matching-destination` | Within a live product operation's documented recovery path, retry materialization with an already matching clean destination. | Matching pinned repository is accepted with the same identity. This checks product workspace recovery, not coordinator restart/resume. |
| `submodules.restore` | Restore the complete clean source hierarchy and repeat ordinary materialization and the fixture simulation. | Fresh successful evidence and exact expected pins; all earlier negative observations remain retained. |
| `submodules.taxi-unchanged` | Compare real Taxi Project checkpoints before/after companion work. | Same accepted source/config/image identities and clean status; companion does not replace any Taxi regression evidence. |
| `submodules.cleanup` | Archive companion manifests/reports and release every owned companion resource. | Ledger reconciled, fixture repositories/runtimes/worktrees/inventory roots removed, Taxi and borrowed state preserved. |

The source-state and selection probes use separate disposable authoring generations
or copied fixtures so they never edit a live Ticket's protected acceptance inputs.
They may use the documented workspace-producing baseline Flow path to avoid
paying for an LLM Ticket per negative fixture. An internal Python function call or
hand-built destination is not evidence that the public product route worked.

The warm-cache negative cases deliberately retain prior build products to catch
stale-success reuse. Archive baseline evidence separately and require a new
invocation/result identity for every observation. Move missing sources to an
operator-owned location outside all consumed source/library paths; do not leave
a renamed module discoverable in the Project. Do not modify source lists or
assertions to make the negative pass, and do not let an agent repair or fetch the
dependency before its failure is captured. These seven functional checks consume
the same 40-minute companion allocation; they do not extend the run deadline.

## Continuation and evidence

The companion requires trustworthy run identity, release/image, authority and
its own fixture setup. It does not depend on Taxi's mutation or fault-repair
outcome. A companion failure blocks only its dependent checks, not independent
Taxi work. At its cap, capture remaining requirements as blocked and clean up.
The whole Taxi core profile cannot pass with an unmet required companion check.

Keep fixture recipe digest, exact source and destination commits, gitlink trees,
local object-closure proofs, isolated variant diffs, Git and Flow/Ticket logs,
resolved source lists, simulation reports, pre-existing sentinel hashes, resource
ledger and cleanup proof outside disposable Project state. Never infer offline
reconstruction from Taxi's symlink or from ordinary cloning.
